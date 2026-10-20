"""Local staging database (sellerboard): schema, import batches, sync bookkeeping.

Tables
* import_batches        one row per import run (scope, counts, validation and sync status)
* import_batch_files    one row per workbook in a batch (hash, headers, counts)
* uk_breakdown_staging  one row per in-scope source row (valid, duplicate or rejected)
* sync_runs             one row per non-dry-run Power BI sync attempt
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import pymysql

from . import schema
from .excel import ImportPlan, StagedRow
from .exceptions import Locked

STAGING_TABLE = "uk_breakdown_staging"
INSERT_CHUNK = 500
IMPORT_LOCK = "sellerboard_import"

BATCHES_DDL = """
CREATE TABLE IF NOT EXISTS import_batches (
  batch_id CHAR(36) NOT NULL,
  fingerprint CHAR(64) NOT NULL COMMENT 'sha256 of file hashes + date range + marketplaces',
  status ENUM('importing','imported','failed') NOT NULL,
  validation_status ENUM('pending','passed','failed') NOT NULL DEFAULT 'pending',
  sync_status ENUM('not_synced','synced','synced_with_conflicts','sync_failed','sync_unverified','local_only')
    NOT NULL DEFAULT 'not_synced' COMMENT 'local_only: imported with --local-only, never sent to Power BI',
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  marketplaces JSON NOT NULL,
  input_dir VARCHAR(1024) NOT NULL,
  file_pattern VARCHAR(255) NOT NULL,
  file_count INT NOT NULL DEFAULT 0,
  rows_read INT NOT NULL DEFAULT 0,
  rows_excluded_date INT NOT NULL DEFAULT 0,
  rows_excluded_marketplace INT NOT NULL DEFAULT 0,
  rows_staged INT NOT NULL DEFAULT 0,
  rows_valid INT NOT NULL DEFAULT 0,
  rows_duplicate INT NOT NULL DEFAULT 0,
  rows_rejected INT NOT NULL DEFAULT 0,
  data_min_date DATE NULL,
  data_max_date DATE NULL,
  summary JSON NULL,
  error_message TEXT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  completed_at DATETIME NULL,
  last_sync_run_id BIGINT UNSIGNED NULL,
  last_synced_at DATETIME NULL,
  PRIMARY KEY (batch_id),
  KEY ix_batches_fingerprint (fingerprint, status),
  KEY ix_batches_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

FILES_DDL = """
CREATE TABLE IF NOT EXISTS import_batch_files (
  file_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  batch_id CHAR(36) NOT NULL,
  source_file VARCHAR(255) NOT NULL,
  source_path VARCHAR(1024) NOT NULL,
  sha256 CHAR(64) NOT NULL,
  size_bytes BIGINT NOT NULL,
  modified_at DATETIME NOT NULL,
  sheet_name VARCHAR(255) NOT NULL,
  headers JSON NOT NULL,
  mapped_columns JSON NOT NULL,
  unknown_headers JSON NOT NULL,
  rows_read INT NOT NULL,
  rows_in_scope INT NOT NULL,
  data_min_date DATE NULL,
  data_max_date DATE NULL,
  PRIMARY KEY (file_id),
  UNIQUE KEY uq_batch_file (batch_id, source_file)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

SYNC_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS sync_runs (
  run_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  batch_id CHAR(36) NOT NULL,
  destination VARCHAR(255) NOT NULL COMMENT 'user@host:port/db.table, never a password',
  update_existing TINYINT(1) NOT NULL,
  status ENUM('running','succeeded','failed','unverified') NOT NULL,
  started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  finished_at DATETIME NULL,
  rows_considered INT NULL,
  rows_inserted INT NULL,
  rows_updated INT NULL,
  rows_unchanged INT NULL,
  rows_conflict INT NULL,
  rows_rejected INT NULL,
  destination_rows_before INT NULL,
  destination_rows_after INT NULL,
  error_message TEXT NULL,
  details JSON NULL,
  PRIMARY KEY (run_id),
  KEY ix_sync_runs_batch (batch_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""


def staging_ddl() -> str:
    data_columns = ",\n  ".join(f"{schema.quote(c.name)} {c.sql_type} NULL" for c in schema.ALL_COLUMNS)
    return f"""
CREATE TABLE IF NOT EXISTS {STAGING_TABLE} (
  staging_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  batch_id CHAR(36) NOT NULL,
  source_file VARCHAR(255) NOT NULL,
  source_row_number INT NOT NULL COMMENT 'Excel row number (header = 1)',
  imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  validation_status ENUM('valid','duplicate','rejected') NOT NULL,
  validation_errors TEXT NULL,
  {data_columns},
  extra_columns JSON NULL COMMENT 'workbook columns not in the catalogue, verbatim',
  raw_row JSON NULL COMMENT 'every source cell of a rejected row',
  sync_status ENUM('pending','inserted','updated','unchanged','conflict','rejected') NOT NULL DEFAULT 'pending',
  sync_run_id BIGINT UNSIGNED NULL,
  synced_at DATETIME NULL,
  grain_key VARCHAR(255) GENERATED ALWAYS AS (
    IF(validation_status = 'valid',
       CONCAT_WS(CHAR(31 USING utf8mb4), `Date`, `Marketplace`, `ASIN`, `SKU`), NULL)) STORED,
  PRIMARY KEY (staging_id),
  UNIQUE KEY uq_staging_source_row (batch_id, source_file, source_row_number),
  UNIQUE KEY uq_staging_batch_grain (batch_id, grain_key),
  KEY ix_staging_grain (`Date`, `Marketplace`, `ASIN`, `SKU`),
  KEY ix_staging_batch_status (batch_id, validation_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""


STAGING_INSERT_COLUMNS: tuple[str, ...] = (
    "batch_id", "source_file", "source_row_number", "validation_status", "validation_errors",
    *(c.name for c in schema.ALL_COLUMNS),
    "extra_columns", "raw_row",
)


class ImportConflict(Locked):
    """Another import holds the import lock."""


@dataclass
class ImportResult:
    batch_id: str
    already_imported: bool = False


def _json(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if value else None


def ensure_schema(conn, log: logging.Logger) -> None:
    """Create the staging tables if missing and add any newly catalogued column.

    DDL commits implicitly in MySQL, so this runs before any data transaction.
    """
    with conn.cursor() as cur:
        for ddl in (BATCHES_DDL, FILES_DDL, staging_ddl(), SYNC_RUNS_DDL):
            cur.execute(ddl)
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (STAGING_TABLE,),
        )
        existing = {name for (name,) in cur.fetchall()}
        cur.execute(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'import_batches' AND COLUMN_NAME = 'sync_status'"
        )
        if "local_only" not in cur.fetchone()[0]:
            cur.execute(
                "ALTER TABLE import_batches MODIFY sync_status ENUM('not_synced','synced','synced_with_conflicts',"
                "'sync_failed','sync_unverified','local_only') NOT NULL DEFAULT 'not_synced'"
            )
            log.info("import_batches.sync_status now allows local_only")
        for column in schema.ALL_COLUMNS:
            if column.name not in existing:
                cur.execute(
                    f"ALTER TABLE {STAGING_TABLE} ADD COLUMN {schema.quote(column.name)} {column.sql_type} NULL"
                )
                log.info("added staging column %s", column.name)
    conn.commit()


def _staging_params(batch_id: str, row: StagedRow) -> tuple:
    return (
        batch_id,
        row.source_file,
        row.source_row_number,
        row.status,
        "; ".join(row.errors) or None,
        *(row.values.get(c.name) for c in schema.ALL_COLUMNS),
        _json(row.extra),
        _json(row.raw),
    )


def write_batch(conn, plan: ImportPlan, log: logging.Logger, *, local_only: bool = False) -> ImportResult:
    """Store ``plan`` as one batch in one transaction; roll back everything on failure.

    Re-running with the same files and scope returns the earlier batch instead of
    staging the rows a second time. ``local_only`` marks the batch so it is never
    sent to Power BI (an earlier, still unsynced batch of the same file is marked too).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, 10)", (IMPORT_LOCK,))
        if cur.fetchone()[0] != 1:
            raise ImportConflict("another import is running (lock sellerboard_import is held)")
    try:
        fingerprint = plan.fingerprint()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT batch_id FROM import_batches WHERE fingerprint = %s AND status = 'imported' "
                "ORDER BY created_at DESC LIMIT 1",
                (fingerprint,),
            )
            found = cur.fetchone()
        if found:
            if local_only:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE import_batches SET sync_status = 'local_only' WHERE batch_id = %s "
                        "AND sync_status IN ('not_synced', 'sync_failed', 'sync_unverified')",
                        (found[0],),
                    )
                conn.commit()
            return ImportResult(found[0], already_imported=True)

        batch_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO import_batches (batch_id, fingerprint, status, start_date, end_date, marketplaces, "
                "input_dir, file_pattern, file_count) VALUES (%s, %s, 'importing', %s, %s, %s, %s, %s, %s)",
                (
                    batch_id, fingerprint, plan.start_date, plan.end_date, json.dumps(list(plan.marketplaces)),
                    str(plan.input_dir), plan.file_pattern, len(plan.files),
                ),
            )
        conn.commit()
        log.info("batch %s: started", batch_id)

        try:
            _write_batch_data(conn, batch_id, plan, log, local_only=local_only)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            log.error("batch %s: rolled back, no staging rows kept: %s", batch_id, exc)
            _mark_failed(conn, batch_id, exc, log)
            raise
        return ImportResult(batch_id)
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", (IMPORT_LOCK,))
            cur.fetchone()


def _write_batch_data(conn, batch_id: str, plan: ImportPlan, log: logging.Logger, *, local_only: bool = False) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO import_batch_files (batch_id, source_file, source_path, sha256, size_bytes, modified_at, "
            "sheet_name, headers, mapped_columns, unknown_headers, rows_read, rows_in_scope, data_min_date, "
            "data_max_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    batch_id, f.path.name, str(f.path), f.sha256, f.size_bytes, f.modified_at, f.sheet_name,
                    json.dumps(f.headers, ensure_ascii=False), json.dumps(f.mapped_columns, ensure_ascii=False),
                    json.dumps(f.unknown_headers, ensure_ascii=False), f.rows_read, len(f.rows),
                    f.data_min_date, f.data_max_date,
                )
                for f in plan.files
            ],
        )

        columns = ", ".join(schema.quote(name, params=True) for name in STAGING_INSERT_COLUMNS)
        placeholders = ", ".join(["%s"] * len(STAGING_INSERT_COLUMNS))
        sql = f"INSERT INTO {STAGING_TABLE} ({columns}) VALUES ({placeholders})"
        rows = plan.rows
        for offset in range(0, len(rows), INSERT_CHUNK):
            chunk = rows[offset : offset + INSERT_CHUNK]
            cur.executemany(sql, [_staging_params(batch_id, row) for row in chunk])
            log.debug("batch %s: staged rows %d-%d", batch_id, offset + 1, offset + len(chunk))

        data_min, data_max = plan.data_dates()
        cur.execute(
            "UPDATE import_batches SET status = 'imported', validation_status = %s, rows_read = %s, "
            "rows_excluded_date = %s, rows_excluded_marketplace = %s, rows_staged = %s, rows_valid = %s, "
            "rows_duplicate = %s, rows_rejected = %s, data_min_date = %s, data_max_date = %s, summary = %s, "
            "sync_status = %s, completed_at = CURRENT_TIMESTAMP WHERE batch_id = %s",
            (
                "passed" if plan.passed else "failed",
                sum(f.rows_read for f in plan.files),
                sum(f.excluded_by_date for f in plan.files),
                sum(sum(f.excluded_marketplaces.values()) for f in plan.files),
                len(rows),
                plan.count("valid"),
                plan.count("duplicate"),
                plan.count("rejected"),
                data_min,
                data_max,
                json.dumps(plan.summary(), ensure_ascii=False, default=str),
                "local_only" if local_only else "not_synced",
                batch_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"batch {batch_id} header row is missing")


def _mark_failed(conn, batch_id: str, exc: Exception, log: logging.Logger) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE import_batches SET status = 'failed', validation_status = 'failed', error_message = %s, "
                "completed_at = CURRENT_TIMESTAMP WHERE batch_id = %s",
                (f"{type(exc).__name__}: {exc}"[:2000], batch_id),
            )
        conn.commit()
    except pymysql.err.MySQLError as mark_exc:  # the connection itself may be gone
        log.error("batch %s: could not record the failure: %s", batch_id, mark_exc)


class StagingRepository:
    """Everything the Power BI sync reads from or records in the local database."""

    def __init__(self, conn):
        self.conn = conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, params)
            rows = list(cur.fetchall())
        self.conn.commit()  # end the read transaction so later reads see fresh data
        return rows

    def get_batch(self, batch_id: str) -> dict | None:
        rows = self._query("SELECT * FROM import_batches WHERE batch_id = %s", (batch_id,))
        if not rows:
            return None
        batch = rows[0]
        batch["marketplaces"] = json.loads(batch["marketplaces"])
        return batch

    def latest_syncable_batch_id(self) -> str | None:
        rows = self._query(
            "SELECT batch_id FROM import_batches WHERE status = 'imported' AND validation_status = 'passed' "
            "AND sync_status <> 'synced' ORDER BY created_at DESC LIMIT 1"
        )
        return rows[0]["batch_id"] if rows else None

    def pending_batch_ids(self) -> list[str]:
        """Validated batches not yet in Power BI, newest first (newest data wins on insert)."""
        rows = self._query(
            "SELECT batch_id FROM import_batches WHERE status = 'imported' AND validation_status = 'passed' "
            "AND sync_status IN ('not_synced', 'sync_failed', 'sync_unverified') ORDER BY created_at DESC"
        )
        return [row["batch_id"] for row in rows]

    def last_synced_end_date(self):
        """The latest end date of a batch that reached Power BI, or None."""
        rows = self._query(
            "SELECT MAX(end_date) AS end_date FROM import_batches "
            "WHERE sync_status IN ('synced', 'synced_with_conflicts')"
        )
        return rows[0]["end_date"] if rows else None

    def list_batches(self, limit: int = 20) -> list[dict]:
        return self._query(
            "SELECT batch_id, created_at, status, validation_status, sync_status, start_date, end_date, "
            "file_count, rows_valid, rows_duplicate, rows_rejected FROM import_batches "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    def load_valid_rows(self, batch_id: str) -> list[dict]:
        columns = ", ".join(schema.quote(c.name, params=True) for c in schema.ALL_COLUMNS)
        return self._query(
            f"SELECT staging_id, source_file, extra_columns, {columns} FROM {STAGING_TABLE} "
            "WHERE batch_id = %s AND validation_status = 'valid' ORDER BY staging_id",
            (batch_id,),
        )

    def file_columns(self, batch_id: str) -> dict[str, list[str]]:
        rows = self._query(
            "SELECT source_file, mapped_columns FROM import_batch_files WHERE batch_id = %s", (batch_id,)
        )
        return {row["source_file"]: json.loads(row["mapped_columns"]) for row in rows}

    def start_sync_run(self, batch_id: str, *, update_existing: bool, destination: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_runs (batch_id, destination, update_existing, status) VALUES (%s, %s, %s, 'running')",
                (batch_id, destination, int(update_existing)),
            )
            run_id = cur.lastrowid
        self.conn.commit()
        return run_id

    def _finish_run(self, cur, run_id: int, status: str, counts: dict, error: str | None, details: Any) -> None:
        cur.execute(
            "UPDATE sync_runs SET status = %s, finished_at = CURRENT_TIMESTAMP, rows_considered = %s, "
            "rows_inserted = %s, rows_updated = %s, rows_unchanged = %s, rows_conflict = %s, rows_rejected = %s, "
            "destination_rows_before = %s, destination_rows_after = %s, error_message = %s, details = %s "
            "WHERE run_id = %s",
            (
                status, counts.get("considered"), counts.get("insert"), counts.get("update"),
                counts.get("unchanged"), counts.get("conflict"), counts.get("rejected"),
                counts.get("destination_rows_before"), counts.get("destination_rows_after"),
                error[:4000] if error else None, _json(details), run_id,
            ),
        )

    def fail_sync_run(self, run_id: int, batch_id: str, *, status: str, batch_status: str, error: str,
                      counts: dict | None = None, details: Any = None) -> None:
        """Record a failed/unverified attempt. Staging rows are left exactly as they were."""
        self.conn.rollback()
        with self.conn.cursor() as cur:
            self._finish_run(cur, run_id, status, counts or {}, error, details)
            cur.execute(
                "UPDATE import_batches SET sync_status = %s, last_sync_run_id = %s WHERE batch_id = %s",
                (batch_status, run_id, batch_id),
            )
        self.conn.commit()

    def complete_sync_run(self, run_id: int, batch_id: str, *, outcomes: dict[int, str], counts: dict,
                          batch_status: str, details: Any) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(
                f"UPDATE {STAGING_TABLE} SET sync_status = %s, sync_run_id = %s, synced_at = CURRENT_TIMESTAMP "
                "WHERE staging_id = %s AND batch_id = %s",
                [(status, run_id, staging_id, batch_id) for staging_id, status in outcomes.items()],
            )
            self._finish_run(cur, run_id, "succeeded", counts, None, details)
            cur.execute(
                "UPDATE import_batches SET sync_status = %s, last_sync_run_id = %s, "
                "last_synced_at = CURRENT_TIMESTAMP WHERE batch_id = %s",
                (batch_status, run_id, batch_id),
            )
        self.conn.commit()
