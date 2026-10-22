#!/usr/bin/env python3
"""Sellerboard UK breakdown -- command-line entry point.

Downloads Sellerboard's "Dashboard by product" export for the last 4 full days
with Playwright (reusing a session a person signed in to once), imports it -- and
any export placed in ``data/incoming/`` by hand -- into the local MySQL staging
database, then inserts the rows Power BI does not have yet into
powerbidb.Uk_breakdown_data. Existing Power BI rows are never changed or deleted.

Running under cron
------------------
Cron calls ``uv run --no-sync python run.py --quiet`` daily at 13:00 from
the project directory (see ``deploy/crontab.example``). Overlapping runs are
refused by an internal lock (exit 11). A missed run is caught up by the next one.

Exit codes
----------
    0   success, including "no files waiting"
    1   unexpected error (see logs/importer.log)
    2   configuration error
    3   Sellerboard sign-in needed -- run --login (a person must do this)
    4   Sellerboard page not as expected (see data/screenshots/)
    6   report download failed or timed out
    7   a report was malformed or had rejected rows (moved to data/failed/)
    9   database error (rolled back; retried next run)
    10  Power BI sync refused or not verified
    11  another run holds the lock
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ folders in the project; set before app/ is imported

from app import __version__, sellerboard
from app.config import load_settings
from app.exceptions import ConfigurationError, PipelineError, SignInNeeded
from app.importer import Importer
from app.logging_config import append_run, configure_logging, get_logger
from app.schedule import report_window

_HELP_EPILOG = """
examples:
  python run.py                     download the last 4 full days, import, sync to Power BI (cron)
  python run.py --login             one-time Sellerboard sign-in (Chrome window, or the terminal on a server)
  python run.py --login --terminal  sign in in the terminal with SELLERBOARD_LOGIN_EMAIL/PASSWORD from .env
  python run.py --fetch-only        download the export into data/incoming/ only
  python run.py --no-fetch          import what is already in data/incoming/, then sync
  python run.py --no-sync           download and import; do not touch Power BI
  python run.py --no-fetch --local-only   import incoming/ into the local DB; never sent to Power BI
  python run.py --preview           show what the next sync would insert (Power BI read-only)
  python run.py --inspect-page      save a screenshot + control list of the Sellerboard dashboard
  python run.py --dry-run           parse and validate only (no Sellerboard, MySQL or moves)
  python run.py --file export.xlsx  import one specific file; it is not moved
  python run.py --init-db           create the staging tables and exit

exit codes:
  0 success · 1 unexpected · 2 config · 3 sign-in needed (run --login) · 4 page changed
  6 download failed · 7 malformed report · 9 database · 10 Power BI sync · 11 locked
"""


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sellerboard-import",
        description="Download Sellerboard 'Dashboard by product' exports, stage them in MySQL and "
                    "insert new rows into powerbidb.Uk_breakdown_data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"sellerboard-import {__version__}")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--file", type=Path, metavar="PATH", action="append",
                       help="Import this file in place (repeatable); it is not moved. Nothing is downloaded.")
    modes.add_argument("--init-db", action="store_true", help="Create the staging tables if missing, then exit.")
    modes.add_argument("--login", action="store_true", help="One-time Sellerboard sign-in by a person.")
    modes.add_argument("--fetch-only", action="store_true", help="Download into incoming/ and stop.")
    modes.add_argument("--inspect-page", action="store_true",
                       help="Save a screenshot and control list of the dashboard to data/screenshots/.")
    modes.add_argument("--preview", action="store_true",
                       help="Show what the sync would insert into Power BI for staged batches; writes nothing.")
    parser.add_argument("--no-fetch", action="store_true", help="Do not download this run; import incoming/ only.")
    parser.add_argument("--update-existing", action="store_true",
                        help="Also update Power BI rows of the window whose values changed in Sellerboard "
                             "(same as SELLERBOARD_UPDATE_EXISTING=true). Nothing is ever deleted.")
    parser.add_argument("--no-sync", action="store_true",
                        help="Do not send to Power BI this run (a later run still sends the batch).")
    parser.add_argument("--local-only", action="store_true",
                        help="Import into the local database and mark the batch so it is never sent to Power BI.")
    parser.add_argument("--start-date", type=_date, help="First report day (default: the 3-day window).")
    parser.add_argument("--end-date", type=_date, help="Last report day (default: yesterday).")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (debugging).")
    parser.add_argument("--terminal", action="store_true",
                        help="With --login: sign in in this terminal with the .env credentials (no window).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and validate only. No Sellerboard, MySQL or file moves.")
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override LOG_LEVEL.")
    parser.add_argument("--quiet", action="store_true", help="Console shows warnings and errors only (cron).")
    parser.add_argument("--no-lock", action="store_true", help="Skip the single-run lock (supervised runs only).")
    return parser


def _has_display() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # the browser session and the exports stay private to this user
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.start_date and args.end_date and args.end_date < args.start_date:
        parser.error("--end-date is before --start-date")
    started = dt.datetime.now()
    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return exc.exit_code
    settings.ensure_directories()
    level = args.log_level or settings.log_level
    configure_logging(settings.log_dir, level=level, console_level="WARNING" if args.quiet else level)
    logger = get_logger("cli")

    code, summary = 1, ""
    try:
        code, summary = _run(args, settings, logger)
    except PipelineError as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        code, summary = exc.exit_code, f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        code = 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected failure")
        code, summary = 1, f"{type(exc).__name__}: {exc}"
    append_run(settings.log_dir, f"{started:%Y-%m-%d %H:%M:%S} exit={code} {' '.join(argv or sys.argv[1:])} | {summary}")
    return code


def _run(args: argparse.Namespace, settings, logger) -> tuple[int, str]:
    if args.init_db:
        Importer(settings).init_database()
        logger.info("Staging tables are ready")
        return 0, "init-db"
    if args.login:
        sellerboard.login(settings, headed=(args.headed or _has_display()) and not args.terminal)
        return 0, "signed in"
    if args.inspect_page:
        shot, controls = sellerboard.inspect_page(settings, headless=not args.headed)
        print(f"Screenshot: {shot}\nControls:   {controls}")
        return 0, "inspected"

    files = None
    if args.file:
        files = [f.expanduser().resolve() for f in args.file]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            logger.error("File(s) not found: %s", ", ".join(missing))
            return 2, "file not found"

    importer = Importer(settings, dry_run=args.dry_run, sync=not (args.no_sync or args.fetch_only or args.local_only),
                        preview=args.preview, use_lock=not args.no_lock, local_only=args.local_only,
                        update_existing=args.update_existing or settings.update_existing)

    def fetch() -> Path:
        if args.start_date and args.end_date:
            start, end = args.start_date, args.end_date
        else:
            start, end = report_window(dt.date.today(), settings.window_days, importer.last_synced_end(),
                                       settings.max_catchup_days)
        logger.info("downloading Sellerboard 'Dashboard by product' %s..%s", start, end)
        try:
            return sellerboard.fetch_report(settings, start, end, headless=not args.headed)
        except SignInNeeded:
            if not (settings.login_email and settings.login_password):
                raise
            logger.warning("Sellerboard session missing or expired: signing in with the .env credentials")
            sellerboard.credential_login(settings, automatic=True)
            return sellerboard.fetch_report(settings, start, end, headless=not args.headed)

    wants_fetch = not (args.no_fetch or args.dry_run or args.preview or files)
    summary = importer.run(files, fetch=fetch if wants_fetch else None, start=args.start_date,
                           end=args.end_date, import_files=not (args.fetch_only or args.preview))
    logger.info("%s", summary.line())
    return summary.exit_code, summary.line()


if __name__ == "__main__":
    sys.exit(main())
