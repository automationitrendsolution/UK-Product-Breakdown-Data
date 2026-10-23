"""US job (``run.py --us-premium``): premiumcarproduct -> powerbidb.Sellerboard_breakdown_data_2024.

The corrected version of the original sellerboard_upload.py. It keeps that script's
rule -- the days in the Excel replace the same days in Power BI -- and makes it safe:

* downloads the Excel itself (Dashboard by product, detailed breakdown, last N full
  days) for the premiumcarproduct account with the saved Sellerboard session
* reads it from data/us/incoming/; the Power BI password comes from .env
* deletes only the file's dates AND marketplaces, and deletes + inserts in ONE
  transaction, with the row count checked before the commit: on any error Power BI
  stays exactly as it was and the Excel stays in incoming/ for the next run
* refuses summary exports (no Commission / ProductCost Sales), which would erase fees
  and COGS; leaves out today's incomplete day and other marketplaces
* keeps every Excel: processed/ after the commit, failed/ if unusable
"""
from __future__ import annotations

import datetime as dt
import math
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pandas as pd
import pymysql

from . import config, db
from .config import Settings
from .exceptions import ConfigurationError, DatabaseError, MalformedReport
from .logging_config import get_logger
from .schedule import RunLock, report_window
from .sync import to_destination

REQUIRED = ("Date", "Marketplace", "ASIN", "SKU", "Commission", "ProductCost Sales")
SB_HEADERS = ("SponsoredBrands", "SponsoredВrands")  # Sellerboard sometimes writes a Cyrillic "В"
INSERT_CHUNK = 500

log = get_logger("us_upload")


def read_file(path: Path, allowed: tuple[str, ...], today: dt.date) -> pd.DataFrame:
    """The rows to upload: full days only, allowed marketplaces only, detailed export only."""
    df = pd.read_excel(path)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise MalformedReport(f"{path.name}: missing {', '.join(missing)} -- export it with "
                              "'Show detailed breakdown of Amazon fees, refund costs and COGS' ticked")
    df = df.rename(columns={h: "SB" for h in SB_HEADERS if h in df.columns})
    df = df.dropna(subset=["Date"])
    try:
        df["Date"] = pd.to_datetime(df["Date"], format="%d/%m/%Y")
    except ValueError as exc:
        raise MalformedReport(f"{path.name}: a Date is not dd/mm/yyyy ({exc})") from None
    partial = int((df["Date"].dt.date >= today).sum())
    if partial:
        log.info("%s: left out %d rows of today's incomplete day", path.name, partial)
    df = df[df["Date"].dt.date < today]
    other = df[~df["Marketplace"].isin(allowed)]
    if len(other):
        log.warning("%s: left out %d rows of other marketplaces: %s", path.name, len(other),
                    sorted(other["Marketplace"].dropna().unique()))
    df = df[df["Marketplace"].isin(allowed)]
    dupes = int(df.duplicated(["Date", "Marketplace", "ASIN", "SKU"], keep=False).sum())
    if dupes:
        raise MalformedReport(f"{path.name}: {dupes} rows repeat the same Date/Marketplace/ASIN/SKU")
    if df.empty:
        raise MalformedReport(f"{path.name}: no rows left for the US marketplaces before today")
    return df


def _table_columns(cur, table: str) -> dict[str, str]:
    cur.execute("SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
    columns = {name: data_type.lower() for name, data_type in cur.fetchall()}
    if not columns:
        raise ConfigurationError(f"table {table} not found in Power BI")
    return columns


def _cell(value, data_type: str):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        value = None
    elif isinstance(value, pd.Timestamp):
        value = value.date()
    elif isinstance(value, float):
        value = Decimal(repr(value))
    if value is None and data_type in ("datetime", "date", "timestamp"):
        return None
    return to_destination(value, data_type)


def replace_days(conn, table: str, df: pd.DataFrame, *, dry_run: bool) -> tuple[int, int]:
    """Replace the file's dates x marketplaces in one transaction. Returns (deleted, inserted)."""
    with conn.cursor() as cur:
        types = _table_columns(cur, table)
    columns = [c for c in df.columns if c in types and c != "id"]
    skipped = [str(c) for c in df.columns if c not in types]
    if skipped:
        log.info("columns with no Power BI column (not stored): %s", ", ".join(skipped))
    dates = sorted({d.strftime("%Y-%m-%d 00:00:00") for d in df["Date"]})
    markets = sorted(df["Marketplace"].unique())
    where = (f"`Date` IN ({', '.join(['%s'] * len(dates))}) "
             f"AND `Marketplace` IN ({', '.join(['%s'] * len(markets))})")
    params = (*dates, *markets)
    rows = [tuple(_cell(value, types[col]) for col, value in zip(columns, record))
            for record in df[columns].itertuples(index=False, name=None)]

    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}` WHERE {where}", params)
        existing = cur.fetchone()[0]
    log.info("%s: %d rows now for %s x %s; file has %d rows", table, existing,
             ", ".join(d[:10] for d in dates), ", ".join(markets), len(rows))
    if dry_run:
        return existing, len(rows)

    names = ", ".join(f"`{c}`".replace("%", "%%") for c in columns)
    insert = f"INSERT INTO `{table}` ({names}) VALUES ({', '.join(['%s'] * len(columns))})"
    conn.begin()
    try:
        with conn.cursor() as cur:
            deleted = cur.execute(f"DELETE FROM `{table}` WHERE {where}", params)
            for start in range(0, len(rows), INSERT_CHUNK):
                cur.executemany(insert, rows[start:start + INSERT_CHUNK])
            cur.execute(f"SELECT COUNT(*) FROM `{table}` WHERE {where}", params)
            now = cur.fetchone()[0]
        if now != len(rows):
            raise DatabaseError(f"after the insert {now} rows match, expected {len(rows)}; rolled back")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return deleted, len(rows)


def _move(path: Path, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    if target.exists():
        target = target.with_name(f"{target.stem}_{dt.datetime.now():%Y%m%d%H%M%S}{target.suffix}")
    shutil.move(str(path), str(target))


def run(args, settings: Settings, download: Callable[[dt.date, dt.date], Path]) -> tuple[int, str]:
    """One US run for run.py (args: no_fetch, fetch_only, dry_run, start_date, end_date, no_lock).
    ``download(start, end)`` exports the Sellerboard report into data/us/incoming/."""
    lock = None if args.no_lock else RunLock(settings.log_dir / ".us-upload.lock")
    if lock:
        lock.__enter__()
    try:
        return _run(args, settings, download)
    finally:
        if lock:
            lock.__exit__(None, None, None)


def _run(args, settings: Settings, download: Callable[[dt.date, dt.date], Path]) -> tuple[int, str]:
    today = dt.date.today()
    if not (args.no_fetch or args.dry_run):
        start, end = (args.start_date, args.end_date) if args.start_date and args.end_date else \
            report_window(today, settings.window_days)
        log.info("downloading premiumcarproduct 'Dashboard by product' %s..%s", start, end)
        download(start, end)
    if args.fetch_only:
        return 0, "downloaded"

    files = sorted(settings.incoming_dir.glob(settings.file_pattern))
    if not files:
        log.info("no files waiting in %s", settings.incoming_dir)
        return 0, "no files"
    table = config.powerbi_table()
    conn = db.connect(config.powerbi_db_config())
    worst, done = 0, []
    try:
        for path in files:
            try:
                df = read_file(path, settings.marketplaces, today)
                deleted, inserted = replace_days(conn, table, df, dry_run=args.dry_run)
            except MalformedReport as exc:
                log.error("%s", exc)
                worst = max(worst, exc.exit_code)
                if not args.dry_run:
                    _move(path, settings.failed_dir)
                continue
            except pymysql.err.MySQLError as exc:
                raise DatabaseError(f"{path.name}: {exc}; nothing changed, the file stays for the next run") from exc
            if args.dry_run:
                log.info("DRY RUN %s: would replace %d rows with %d", path.name, deleted, inserted)
            else:
                log.info("%s: replaced %d rows with %d (committed)", path.name, deleted, inserted)
                _move(path, settings.processed_dir)
            done.append(f"{path.name}: -{deleted}/+{inserted}")
    finally:
        conn.close()
    return worst, "; ".join(done) or "nothing uploaded"
