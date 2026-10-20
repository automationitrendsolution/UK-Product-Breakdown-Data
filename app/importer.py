"""One run: import Sellerboard exports into local staging, then sync new rows to Power BI.

* Every workbook is its own batch, so two downloads for overlapping days never mix.
* Files from data/incoming/ move to processed/ (imported, or already imported) or
  failed/ (unusable, or rows rejected). Files given with --file are never moved.
  A database error leaves the file in incoming/ for the next run.
* The scope of a file is --start-date/--end-date when given, otherwise the period
  in its Sellerboard file name, never later than yesterday (today is incomplete).
* Power BI: every validated batch not yet synced is sent, newest first, insert-only
  (rows already in Power BI are never changed; see app/sync.py).
* Direct mode: with no staging database configured (SELLERBOARD_DB_* unset), each
  checked file goes straight to the same insert-only, verified Power BI sync; the
  file stays in incoming/ until that succeeds, so a Power BI outage is retried.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config, db, excel, schema, staging
from .config import Settings
from .exceptions import ConfigurationError
from .logging_config import get_logger
from .schedule import RunLock
from .sync import MySQLDestination, SyncOutcome, SyncRefused, SyncUnverified, run_sync

log = get_logger("importer")
EARLIEST = dt.date(2000, 1, 1)


@dataclass
class RunSummary:
    files: Counter = field(default_factory=Counter)  # imported | already | failed | checked
    rows_staged: int = 0
    powerbi: Counter = field(default_factory=Counter)  # insert | unchanged | conflict | rejected
    exit_code: int = 0

    def line(self) -> str:
        files = " ".join(f"{k}={v}" for k, v in sorted(self.files.items())) or "none"
        synced = " ".join(f"{k}={v}" for k, v in sorted(self.powerbi.items())) or "none"
        return f"files: {files} | rows staged: {self.rows_staged} | power bi: {synced}"


def scope_for(path: Path, start: dt.date | None, end: dt.date | None, today: dt.date) -> tuple[dt.date, dt.date]:
    yesterday = today - dt.timedelta(days=1)
    if start and end:
        return start, end
    period = excel.filename_period(path.name)
    if period:
        return start or period[0], end or min(period[1], yesterday)
    return start or EARLIEST, end or yesterday


class Importer:
    def __init__(self, settings: Settings, *, dry_run: bool = False, sync: bool = True,
                 preview: bool = False, use_lock: bool = True, local_only: bool = False):
        self.settings = settings
        self.dry_run = dry_run
        self.sync = sync and not dry_run
        self.preview = preview
        self.local_only = local_only
        self.use_lock = use_lock
        self.direct = not config.staging_configured()

    # ------------------------------------------------------------------ entry points

    def init_database(self) -> None:
        if self.direct:
            raise ConfigurationError("no staging database is configured (SELLERBOARD_DB_* not set), so there is "
                                     "nothing to create: runs send checked exports straight to Power BI")
        conn = db.connect(config.local_db_config())
        try:
            staging.ensure_schema(conn, log)
        finally:
            conn.close()

    def last_synced_end(self) -> dt.date | None:
        if self.direct:  # the files that reached Power BI are the history
            ends = []
            for path in self.settings.processed_dir.glob("*.xlsx"):
                period = excel.filename_period(path.name)
                if period:
                    downloaded = dt.date.fromtimestamp(path.stat().st_mtime)
                    ends.append(min(period[1], downloaded - dt.timedelta(days=1)))
            return max(ends) if ends else None
        conn = db.connect(config.local_db_config())
        try:
            staging.ensure_schema(conn, log)
            return staging.StagingRepository(conn).last_synced_end_date()
        finally:
            conn.close()

    def run(self, files: list[Path] | None, *, fetch: Callable[[], Path] | None = None,
            start: dt.date | None = None, end: dt.date | None = None, import_files: bool = True) -> RunSummary:
        lock = RunLock(self.settings.log_dir / ".run.lock") if self.use_lock else None
        if lock:
            lock.__enter__()
        try:
            summary = RunSummary()
            if self.direct:
                if self.local_only:
                    raise ConfigurationError("--local-only needs a staging database (SELLERBOARD_DB_* in .env)")
                log.info("no staging database configured: checked exports go straight to Power BI")
            if fetch:
                fetch()
            if import_files or (self.preview and self.direct):
                in_place = files is not None
                targets = files if in_place else excel.find_input_files(self.settings.incoming_dir,
                                                                        self.settings.file_pattern)
                if not targets and not in_place:
                    log.info("no files waiting in %s", self.settings.incoming_dir)
                for path in targets:
                    self._import_file(path, start, end, move=not in_place, summary=summary)
            if (self.sync or self.preview) and not self.direct:
                self._sync_pending(summary)
            return summary
        finally:
            if lock:
                lock.__exit__(None, None, None)

    # ------------------------------------------------------------------ import

    def _move(self, path: Path, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / path.name
        if target.exists():
            target = target.with_name(f"{target.stem}_{dt.datetime.now():%Y%m%d%H%M%S}{target.suffix}")
        shutil.move(str(path), str(target))
        log.info("moved %s to %s/", path.name, folder.name)

    def _import_file(self, path: Path, start, end, *, move: bool, summary: RunSummary) -> None:
        scope_start, scope_end = scope_for(path, start, end, dt.date.today())
        try:
            plan = excel.build_plan([path], scope_start, scope_end, self.settings.marketplaces,
                                    path.parent, path.name)
        except excel.InputError as exc:
            log.error("%s", exc)
            summary.files["failed"] += 1
            summary.exit_code = max(summary.exit_code, exc.exit_code)
            if move and not self.dry_run:
                self._move(path, self.settings.failed_dir)
            return
        if scope_start == EARLIEST:  # no period in the name: the file's own dates
            data_min, data_max = plan.data_dates()
            plan.start_date = max(scope_start, data_min or scope_start)
            plan.end_date = min(scope_end, data_max or scope_end)

        log_plan(plan)
        if self.dry_run:
            summary.files["checked"] += 1
            if not plan.passed:
                summary.exit_code = max(summary.exit_code, 7)
            return
        if self.direct:
            self._send_direct(path, plan, move=move, summary=summary)
            return

        conn = db.connect(config.local_db_config())  # DatabaseUnavailable: the file stays in incoming/
        try:
            staging.ensure_schema(conn, log)
            result = staging.write_batch(conn, plan, log, local_only=self.local_only)
        finally:
            conn.close()

        if result.already_imported:
            log.info("%s: already imported as batch %s", path.name, result.batch_id)
            summary.files["already"] += 1
        elif plan.passed:
            log.info("%s: batch %s staged %d rows%s", path.name, result.batch_id, plan.count("valid"),
                     " (local only: never sent to Power BI)" if self.local_only else "")
            summary.files["imported"] += 1
            summary.rows_staged += plan.count("valid")
        else:
            log.error("%s: batch %s failed validation (%d rejected rows); it will not be synced",
                      path.name, result.batch_id, plan.count("rejected"))
            summary.files["failed"] += 1
            summary.exit_code = max(summary.exit_code, 7)
        if move:
            self._move(path, self.settings.processed_dir if plan.passed else self.settings.failed_dir)

    # ------------------------------------------------------------------ Power BI

    def _destination(self):
        powerbi = config.powerbi_db_config()
        table = config.powerbi_table()
        destination = f"{powerbi.describe()}.{table}"

        def open_destination(read_only: bool) -> MySQLDestination:
            return MySQLDestination(db.connect(powerbi, found_rows=True), table,
                                    read_only=read_only, description=destination)

        return open_destination, destination

    def _sync_batch(self, repo, batch_id: str, summary: RunSummary) -> SyncOutcome:
        open_destination, destination = self._destination()
        log.info("Power BI %s: batch %s -> %s", "preview" if self.preview else "sync", batch_id, destination)
        outcome = run_sync(repo, open_destination, batch_id, dry_run=self.preview,
                           update_existing=False, destination_label=destination, log=log)
        log_sync(outcome)
        for key in ("insert", "unchanged", "conflict", "rejected"):
            summary.powerbi[key] += outcome.plan.counts()[key]
        return outcome

    def _sync_pending(self, summary: RunSummary) -> None:
        conn = db.connect(config.local_db_config())
        try:
            staging.ensure_schema(conn, log)
            repo = staging.StagingRepository(conn)
            pending = repo.pending_batch_ids()
            if not pending:
                log.info("nothing waiting for Power BI")
                return
            for batch_id in pending:
                try:
                    self._sync_batch(repo, batch_id, summary)
                except (SyncRefused, SyncUnverified) as exc:
                    log.error("batch %s: %s", batch_id, exc)
                    summary.exit_code = max(summary.exit_code, exc.exit_code)
        finally:
            conn.close()

    def _send_direct(self, path: Path, plan: excel.ImportPlan, *, move: bool, summary: RunSummary) -> None:
        """Direct mode: one checked file straight to Power BI. A database error leaves
        the file in incoming/ (retried next run); only a verified sync moves it."""
        if not plan.passed:
            log.error("%s: %d rejected rows; nothing sent to Power BI", path.name, plan.count("rejected"))
            summary.files["failed"] += 1
            summary.exit_code = max(summary.exit_code, 7)
            if move and not self.preview:
                self._move(path, self.settings.failed_dir)
            return
        repo = PlanRepository(plan)
        try:
            self._sync_batch(repo, repo.batch_id, summary)
        except SyncRefused as exc:
            log.error("%s: %s", path.name, exc)
            summary.files["failed"] += 1
            summary.exit_code = max(summary.exit_code, exc.exit_code)
            if move and not self.preview:
                self._move(path, self.settings.failed_dir)
            return
        except SyncUnverified as exc:  # possibly committed: the next run re-checks it (rows then "unchanged")
            log.error("%s: %s; the file stays in incoming/ and is checked again next run", path.name, exc)
            summary.exit_code = max(summary.exit_code, exc.exit_code)
            return
        if self.preview:
            summary.files["checked"] += 1
            return
        summary.files["imported"] += 1
        summary.rows_staged += plan.count("valid")
        if move:
            self._move(path, self.settings.processed_dir)


class PlanRepository:
    """What the Power BI sync reads from staging, served from one in-memory checked
    file (direct mode). Nothing is stored: the file itself is the record."""

    def __init__(self, plan: excel.ImportPlan):
        self.batch_id = "file-" + plan.fingerprint()[:12]
        valid = [row for row in plan.rows if row.status == "valid"]
        self._rows = [
            {"staging_id": number, "source_file": row.source_file,
             "extra_columns": json.dumps(row.extra, ensure_ascii=False) if row.extra else None,
             **{column.name: row.values.get(column.name) for column in schema.ALL_COLUMNS}}
            for number, row in enumerate(valid, 1)
        ]
        self._batch = {"batch_id": self.batch_id, "status": "imported", "validation_status": "passed",
                       "start_date": plan.start_date, "end_date": plan.end_date,
                       "marketplaces": list(plan.marketplaces), "rows_valid": len(valid),
                       "rows_rejected": plan.count("rejected")}
        self._files = {parsed.path.name: parsed.mapped_columns for parsed in plan.files}

    def get_batch(self, batch_id: str) -> dict | None:
        return dict(self._batch) if batch_id == self.batch_id else None

    def load_valid_rows(self, batch_id: str) -> list[dict]:
        return self._rows

    def file_columns(self, batch_id: str) -> dict[str, list[str]]:
        return self._files

    def start_sync_run(self, batch_id: str, *, update_existing: bool, destination: str) -> int:
        return 1

    def fail_sync_run(self, *args, **kwargs) -> None:
        pass

    def complete_sync_run(self, *args, **kwargs) -> None:
        pass


# ---------------------------------------------------------------------- reporting


def log_plan(plan: excel.ImportPlan) -> None:
    for parsed in plan.files:
        log.info("%s: %d rows read, %d in scope %s..%s, %d outside the dates; marketplaces %s",
                 parsed.path.name, parsed.rows_read, len(parsed.rows), plan.start_date, plan.end_date,
                 parsed.excluded_by_date, dict(parsed.marketplace_counts.most_common()))
        if parsed.excluded_marketplaces:
            log.warning("  excluded (marketplace not in scope): %s", dict(parsed.excluded_marketplaces))
    summary = plan.summary()
    log.info("rows: %d valid, %d duplicate, %d rejected; %d distinct ASINs", summary["rows_valid"],
             summary["rows_duplicate"], summary["rows_rejected"], summary["distinct_asins"])
    for row in plan.rows:
        if row.status == "rejected":
            log.error("rejected: %s row %d: %s", row.source_file, row.source_row_number, "; ".join(row.errors))
        elif row.status == "duplicate":
            log.warning("duplicate: %s row %d (%s)", row.source_file, row.source_row_number, "; ".join(row.errors))
    for warning in plan.warnings:
        log.warning(warning)


def log_sync(outcome: SyncOutcome) -> None:
    plan = outcome.plan
    log.info("%-10s  %-14s  %7s  %6s  %6s  %9s  %8s  %8s", "date", "marketplace", "in dest", "staged",
             "insert", "unchanged", "conflict", "rejected")
    for day, market, existing, staged, insert, _update, unchanged, conflict, rejected in plan.by_date_marketplace():
        log.info("%-10s  %-14s  %7d  %6d  %6d  %9d  %8d  %8d", day, market, existing, staged, insert,
                 unchanged, conflict, rejected)
    for action in plan.of("conflict")[:5]:
        changes = ", ".join(f"{c}: {s} vs {d!r}" for c, s, d in action.differences[:3])
        log.info("conflict (left unchanged) %s: %s", action.label, changes)
    c = plan.counts()
    if outcome.dry_run:
        log.info("PREVIEW (nothing written): would insert %d; unchanged %d, conflict %d, rejected %d",
                 c["insert"], c["unchanged"], c["conflict"], c["rejected"])
    else:
        e = outcome.execution
        log.info("SYNCED AND VERIFIED: inserted %d, unchanged %d, conflict %d (left as they are), rejected %d; "
                 "Power BI rows in range %d -> %d", e["inserted"], c["unchanged"], c["conflict"], c["rejected"],
                 e["destination_rows_before"], e["destination_rows_after"])
