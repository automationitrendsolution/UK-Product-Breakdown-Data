"""Controlled sync of one validated staging batch into powerbidb.Uk_breakdown_data.

Uk_breakdown_data has no unique key (only an auto-increment ``id``), so duplicate
protection is done here on (Date, Marketplace, ASIN, SKU):

* key not in the destination              -> insert
* key present once with the same values   -> unchanged (nothing written)
* key present once with different values  -> conflict, left untouched; updated only
                                             with ``update_existing`` (the explicit,
                                             opt-in update rule)
* key present more than once              -> rejected as ambiguous, never touched

Nothing is deleted or truncated. All writes run in one destination transaction that
is rolled back on any error, and a sync is reported successful only after a fresh
read confirms every written row and every pre-existing row in the date range.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from itertools import groupby
from typing import Any, Callable

import pymysql

from . import schema
from .exceptions import DatabaseError, SyncError

TEXT_TYPES = frozenset({"char", "varchar", "tinytext", "text", "mediumtext", "longtext"})
REAL_TYPES = frozenset({"float", "double", "decimal"})
INT_TYPES = frozenset({"tinyint", "smallint", "mediumint", "int", "bigint"})
DATE_TYPES = frozenset({"date", "datetime", "timestamp"})

ABS_TOLERANCE = Decimal("0.005")  # values are money at 2 dp, counts and percentages
REL_TOLERANCE = Decimal("0.000001")  # FLOAT columns keep ~7 significant digits
INSERT_CHUNK = 500
SYNC_LOCK = "sellerboard_uk_breakdown_sync"

KEY_NAMES = schema.KEY_NAMES
COLUMN_MAP = schema.DESTINATION_COLUMN_MAP
DESTINATION_COLUMNS: tuple[str, ...] = tuple(COLUMN_MAP.values())
OUTCOME = {"insert": "inserted", "update": "updated", "unchanged": "unchanged", "conflict": "conflict", "rejected": "rejected"}


class SyncRefused(SyncError):
    """The batch or the destination failed a pre-check; nothing was written."""


class SyncFailed(DatabaseError):
    """The write transaction was rolled back; the destination is unchanged."""


class SyncUnverified(SyncError):
    """Changes may have been committed but could not be verified. Re-run the sync."""


# --------------------------------------------------------------------------- values


def format_text_number(value: Decimal | int) -> str:
    """Numbers bound for text columns, written as the existing rows are: '2', '-2.74'."""
    if isinstance(value, int):
        return str(value)
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def to_destination(value: Any, data_type: str) -> Any:
    if data_type in DATE_TYPES:
        return value if data_type == "date" else dt.datetime.combine(value, dt.time())
    if data_type in TEXT_TYPES:
        if value is None:
            return ""  # how the existing loader stores an empty cell in a text column
        if isinstance(value, (Decimal, int)):
            return format_text_number(value)
        return str(value)
    if data_type in REAL_TYPES:
        return value
    if data_type in INT_TYPES:
        if value is None:
            return None
        number = Decimal(value)
        if number != number.to_integral_value():
            raise ValueError(f"{value} is not a whole number but the destination column is {data_type}")
        return int(number)
    raise ValueError(f"unsupported destination column type {data_type!r}")


def _comparable(value: Any) -> Decimal | str:
    """Blank, NULL and 0 compare equal: Sellerboard leaves zero cells empty."""
    if value is None:
        return Decimal(0)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return Decimal(0)
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    return number if number.is_finite() else text


def values_equal(a: Any, b: Any) -> bool:
    x, y = _comparable(a), _comparable(b)
    if isinstance(x, Decimal) and isinstance(y, Decimal):
        return abs(x - y) <= max(ABS_TOLERANCE, REL_TOLERANCE * max(abs(x), abs(y)))
    return str(x) == str(y)


def key_of(row: dict) -> tuple:
    """Normalised grain; MySQL compares these strings case-insensitively."""
    day = row["Date"]
    if isinstance(day, dt.datetime):
        day = day.date()
    return (day, *(str(row[name] or "").strip().casefold() for name in KEY_NAMES[1:]))


def _describe_key(key: tuple) -> str:
    return " / ".join(str(part) for part in key)


# --------------------------------------------------------------------------- destination


class Destination:
    """The operations the sync needs from the destination table."""

    description = "destination"

    def columns(self) -> dict[str, str]:
        raise NotImplementedError

    def fetch_rows(self, start: dt.date, end: dt.date, columns: tuple[str, ...]) -> list[dict]:
        raise NotImplementedError

    def count_rows(self, start: dt.date, end: dt.date) -> int:
        raise NotImplementedError

    def acquire_lock(self) -> bool:
        raise NotImplementedError

    def release_lock(self) -> None:
        raise NotImplementedError

    def begin(self) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError

    def rollback(self) -> None:
        raise NotImplementedError

    def insert_rows(self, columns: tuple[str, ...], rows: list[tuple]) -> int:
        raise NotImplementedError

    def update_row(self, row_id: int, key_values: dict[str, Any], assignments: dict[str, Any]) -> int:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class MySQLDestination(Destination):
    def __init__(self, conn, table: str, *, read_only: bool, description: str = ""):
        self.conn = conn
        self.table = schema.quote(table)
        self.table_name = table
        self.description = description or table
        with conn.cursor() as cur:
            # A dry run cannot write even by mistake: the server rejects it.
            cur.execute("SET SESSION TRANSACTION READ ONLY" if read_only else "SET SESSION TRANSACTION READ WRITE")

    def columns(self) -> dict[str, str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (self.table_name,),
            )
            result = {name: data_type.lower() for name, data_type in cur.fetchall()}
        self.conn.commit()
        return result

    def _range(self, start: dt.date, end: dt.date) -> tuple:
        return (start, end + dt.timedelta(days=1))

    def fetch_rows(self, start, end, columns):
        selected = ", ".join(schema.quote(c, params=True) for c in ("id", *columns))
        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT {selected} FROM {self.table} WHERE `Date` >= %s AND `Date` < %s ORDER BY `id`",
                self._range(start, end),
            )
            return list(cur.fetchall())

    def count_rows(self, start, end):
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {self.table} WHERE `Date` >= %s AND `Date` < %s", self._range(start, end))
            return cur.fetchone()[0]

    def acquire_lock(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 0)", (SYNC_LOCK,))
            return cur.fetchone()[0] == 1

    def release_lock(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", (SYNC_LOCK,))
            cur.fetchone()

    def begin(self):
        self.conn.begin()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def insert_rows(self, columns, rows):
        names = ", ".join(schema.quote(c, params=True) for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        with self.conn.cursor() as cur:
            cur.executemany(f"INSERT INTO {self.table} ({names}) VALUES ({placeholders})", rows)
            return cur.rowcount

    def update_row(self, row_id, key_values, assignments):
        sets = ", ".join(f"{schema.quote(c, params=True)} = %s" for c in assignments)
        guard = " AND ".join(f"{schema.quote(c, params=True)} = %s" for c in key_values)
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {self.table} SET {sets} WHERE `id` = %s AND {guard}",
                (*assignments.values(), row_id, *key_values.values()),
            )
            return cur.rowcount  # matched rows: the connection uses CLIENT.FOUND_ROWS

    def close(self):
        self.conn.close()


# --------------------------------------------------------------------------- planning


@dataclass
class RowAction:
    staging_id: int
    key: tuple
    marketplace: str  # as spelled in the source, for reporting
    label: str  # Date / Marketplace / ASIN / SKU as spelled in the source
    action: str  # insert | update | unchanged | conflict | rejected
    values: dict[str, Any] = field(default_factory=dict)  # every column the export provides, converted
    columns: tuple[str, ...] = ()  # the columns this action writes
    dest_row: dict | None = None
    differences: list[tuple[str, Any, Any]] = field(default_factory=list)  # column, staging, destination
    reason: str | None = None


@dataclass
class SyncPlan:
    batch_id: str
    start: dt.date
    end: dt.date
    actions: list[RowAction]
    existing_rows: list[dict]
    unmapped_values: dict[str, int]
    destination_unfilled_columns: list[str]

    def of(self, action: str) -> list[RowAction]:
        return [a for a in self.actions if a.action == action]

    def counts(self) -> dict[str, int]:
        tally = Counter(a.action for a in self.actions)
        return {"considered": len(self.actions), **{k: tally[k] for k in OUTCOME}}

    def by_date_marketplace(self) -> list[tuple]:
        existing = Counter((key_of(r)[0], str(r["Marketplace"]).strip()) for r in self.existing_rows)
        tally: dict[tuple, Counter] = defaultdict(Counter)
        for a in self.actions:
            tally[(a.key[0], a.marketplace)][a.action] += 1
        groups = sorted(set(tally) | set(existing), key=lambda g: (g[0], g[1]))
        return [
            (day, market, existing[(day, market)], sum(tally[(day, market)].values()),
             *(tally[(day, market)][k] for k in OUTCOME))
            for day, market in groups
        ]

    def details(self) -> dict:
        return {
            "conflicts": [
                {"key": a.label, "dest_id": a.dest_row["id"], "columns": [d[0] for d in a.differences]}
                for a in self.of("conflict")
            ][:200],
            "updated": [
                {"key": a.label, "dest_id": a.dest_row["id"],
                 "changes": {c: [str(s), str(d)] for c, s, d in a.differences}}
                for a in self.of("update")
            ][:200],
            "rejected": [{"key": a.label, "reason": a.reason} for a in self.of("rejected")][:200],
            "unmapped_values": self.unmapped_values,
        }


def plan_sync(
    batch: dict,
    rows: list[dict],
    file_columns: dict[str, list[str]],
    dest_columns: dict[str, str],
    existing_rows: list[dict],
    *,
    update_existing: bool,
) -> SyncPlan:
    missing = [c for c in DESTINATION_COLUMNS if c not in dest_columns]
    if missing:
        raise SyncRefused(
            "the destination table no longer has mapped column(s) " + ", ".join(missing)
            + "; review app/schema.py against the live schema"
        )

    index: dict[tuple, list[dict]] = defaultdict(list)
    for existing in existing_rows:
        index[key_of(existing)].append(existing)

    mapped_sources = set(COLUMN_MAP)
    unmapped: Counter = Counter()
    actions: list[RowAction] = []
    for row in rows:
        present = set(file_columns.get(row["source_file"], ()))
        if not set(KEY_NAMES) <= present:
            raise SyncRefused(f"no column list recorded for {row['source_file']}; re-import the batch")
        for column in schema.VALUE_COLUMNS:
            if column.name not in mapped_sources and not values_equal(row.get(column.name), None):
                unmapped[column.name] += 1
        for name in json.loads(row["extra_columns"]) if row.get("extra_columns") else ():
            unmapped[f"{name} (unrecognised)"] += 1

        label = " / ".join(str(row[name]) for name in KEY_NAMES)
        action = RowAction(row["staging_id"], key_of(row), row["Marketplace"], label, "insert")
        try:
            action.values = {
                dest: to_destination(row[source], dest_columns[dest])
                for source, dest in COLUMN_MAP.items()
                if source in present
            }
        except ValueError as exc:
            action.action, action.reason = "rejected", str(exc)
            actions.append(action)
            continue
        action.columns = tuple(action.values)

        matches = index.get(action.key, [])
        if len(matches) > 1:
            action.action = "rejected"
            action.reason = f"{len(matches)} destination rows already have this Date/Marketplace/ASIN/SKU"
        elif matches:
            current = matches[0]
            action.dest_row = current
            action.differences = [
                (dest, row[source], current[dest])
                for source, dest in COLUMN_MAP.items()
                if source in present and source not in KEY_NAMES and not values_equal(action.values[dest], current[dest])
            ]
            if not action.differences:
                action.action = "unchanged"
            elif update_existing:
                action.action = "update"
                action.columns = tuple(d[0] for d in action.differences)
            else:
                action.action = "conflict"
        actions.append(action)

    filled = {c for a in actions for c in a.values}
    unfilled = [c for c in dest_columns if c != "id" and c not in filled]
    return SyncPlan(
        batch_id=batch["batch_id"],
        start=batch["start_date"],
        end=batch["end_date"],
        actions=actions,
        existing_rows=existing_rows,
        unmapped_values=dict(sorted(unmapped.items())),
        destination_unfilled_columns=unfilled,
    )


# --------------------------------------------------------------------------- execution


def execute_plan(dest: Destination, plan: SyncPlan, log: logging.Logger) -> dict[str, int]:
    """Apply the plan's inserts and updates in one transaction, or roll everything back."""
    inserts, updates = plan.of("insert"), plan.of("update")
    if not inserts and not updates:
        count = dest.count_rows(plan.start, plan.end)
        return {"inserted": 0, "updated": 0, "destination_rows_before": count, "destination_rows_after": count}

    if not dest.acquire_lock():
        raise SyncFailed("another sync is running (lock sellerboard_uk_breakdown_sync is held); nothing was written")
    try:
        dest.begin()
        try:
            current = dest.fetch_rows(plan.start, plan.end, KEY_NAMES)
            if Counter(key_of(r) for r in current) != Counter(key_of(r) for r in plan.existing_rows):
                raise SyncFailed("the destination changed after the sync was planned; nothing was written, run it again")
            before = len(current)

            inserted = 0
            by_columns = sorted(inserts, key=lambda a: a.columns)
            for columns, group in groupby(by_columns, key=lambda a: a.columns):
                group = list(group)
                for offset in range(0, len(group), INSERT_CHUNK):
                    chunk = group[offset : offset + INSERT_CHUNK]
                    inserted += dest.insert_rows(columns, [tuple(a.values[c] for c in columns) for a in chunk])
            if inserted != len(inserts):
                raise SyncFailed(f"inserted {inserted} rows, expected {len(inserts)}")

            for action in updates:
                matched = dest.update_row(
                    action.dest_row["id"],
                    {name: action.dest_row[name] for name in KEY_NAMES},
                    {c: action.values[c] for c in action.columns},
                )
                if matched != 1:
                    raise SyncFailed(f"update of destination id {action.dest_row['id']} matched {matched} rows")

            after = dest.count_rows(plan.start, plan.end)
            if after != before + inserted:
                raise SyncFailed(f"row count check failed: {before} + {inserted} inserted != {after}")
            dest.commit()
        except BaseException:
            dest.rollback()
            raise
    finally:
        dest.release_lock()
    log.info("destination: committed %d inserts and %d updates", inserted, len(updates))
    return {"inserted": inserted, "updated": len(updates), "destination_rows_before": before, "destination_rows_after": after}


def verify(dest: Destination, plan: SyncPlan) -> list[str]:
    """Re-read the destination: every synced key exactly once with the staged values,
    and every row that existed before untouched (unless it was an approved update)."""
    rows = dest.fetch_rows(plan.start, plan.end, DESTINATION_COLUMNS)
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    by_id = {}
    for row in rows:
        by_key[key_of(row)].append(row)
        by_id[row["id"]] = row

    problems: list[str] = []
    for action in plan.actions:
        if action.action not in ("insert", "update", "unchanged"):
            continue
        matches = by_key.get(action.key, [])
        if len(matches) != 1:
            problems.append(f"{action.label}: expected 1 destination row, found {len(matches)}")
            continue
        wrong = [c for c, v in action.values.items() if not values_equal(v, matches[0][c])]
        if wrong:
            problems.append(f"{action.label}: destination differs in {', '.join(wrong)}")

    updated_ids = {a.dest_row["id"] for a in plan.of("update")}
    for before in plan.existing_rows:
        if before["id"] in updated_ids:
            continue
        after = by_id.get(before["id"])
        if after is None:
            problems.append(f"pre-existing destination row id {before['id']} is missing")
        elif after != before:
            problems.append(f"pre-existing destination row id {before['id']} changed")
    return problems


# --------------------------------------------------------------------------- orchestration


@dataclass
class SyncOutcome:
    plan: SyncPlan
    dry_run: bool
    status: str  # planned | synced | synced_with_conflicts
    run_id: int | None = None
    execution: dict[str, int] = field(default_factory=dict)


def _check_batch(batch: dict | None, batch_id: str) -> dict:
    if batch is None:
        raise SyncRefused(f"batch {batch_id} does not exist in the local staging database")
    if batch["status"] != "imported":
        raise SyncRefused(f"batch {batch_id} has import status {batch['status']!r}; only imported batches are synced")
    if batch["validation_status"] != "passed":
        raise SyncRefused(
            f"batch {batch_id} failed validation ({batch['rows_rejected']} rejected rows); "
            "fix the source export and import it again"
        )
    return batch


def _check_rows(batch: dict, rows: list[dict]) -> None:
    if len(rows) != batch["rows_valid"]:
        raise SyncRefused(f"staging holds {len(rows)} valid rows for the batch but the import recorded {batch['rows_valid']}")
    if not rows:
        raise SyncRefused("the batch has no valid rows")
    allowed = set(batch["marketplaces"])
    seen: set[tuple] = set()
    for row in rows:
        where = f"staging_id {row['staging_id']}"
        if not batch["start_date"] <= row["Date"] <= batch["end_date"]:
            raise SyncRefused(f"{where}: date {row['Date']} is outside the batch range")
        if row["Marketplace"] not in allowed:
            raise SyncRefused(f"{where}: marketplace {row['Marketplace']!r} is not in the batch scope")
        if not row["ASIN"] or not row["SKU"]:
            raise SyncRefused(f"{where}: ASIN and SKU are required")
        key = key_of(row)
        if key in seen:
            raise SyncRefused(f"{where}: duplicate Date/Marketplace/ASIN/SKU {_describe_key(key)}")
        seen.add(key)


def run_sync(
    repo,
    open_destination: Callable[[bool], Destination],
    batch_id: str,
    *,
    dry_run: bool,
    update_existing: bool,
    destination_label: str,
    log: logging.Logger,
) -> SyncOutcome:
    batch = _check_batch(repo.get_batch(batch_id), batch_id)
    rows = repo.load_valid_rows(batch_id)
    _check_rows(batch, rows)
    file_columns = repo.file_columns(batch_id)

    run_id = None if dry_run else repo.start_sync_run(
        batch_id, update_existing=update_existing, destination=destination_label
    )
    plan: SyncPlan | None = None
    execution: dict[str, int] = {}
    try:
        dest = open_destination(dry_run)
        try:
            dest_columns = dest.columns()
            if not dest_columns:
                raise SyncRefused(f"destination table {destination_label} was not found")
            existing = dest.fetch_rows(batch["start_date"], batch["end_date"], DESTINATION_COLUMNS)
            plan = plan_sync(batch, rows, file_columns, dest_columns, existing, update_existing=update_existing)
            if dry_run:
                return SyncOutcome(plan, True, "planned", execution={"destination_rows_before": len(existing)})
            execution = execute_plan(dest, plan, log)
            problems = verify(dest, plan)
        finally:
            dest.close()
    except Exception as exc:
        committed = bool(execution.get("inserted") or execution.get("updated"))
        if run_id is not None:
            _record_failure(repo, run_id, batch_id, committed, f"{type(exc).__name__}: {exc}",
                            _counts(plan, execution) if plan else None, log)
        if committed:
            raise SyncUnverified(f"changes were committed but could not be verified ({exc}); run the sync again") from exc
        raise

    counts = _counts(plan, execution)
    if problems:
        _record_failure(repo, run_id, batch_id, True, "; ".join(problems[:20]), counts, log, details={"problems": problems[:200]})
        raise SyncUnverified(f"destination verification failed: {'; '.join(problems[:5])}")

    status = "synced_with_conflicts" if counts["conflict"] or counts["rejected"] else "synced"
    repo.complete_sync_run(
        run_id, batch_id,
        outcomes={a.staging_id: OUTCOME[a.action] for a in plan.actions},
        counts=counts, batch_status=status, details=plan.details(),
    )
    return SyncOutcome(plan, False, status, run_id, execution)


def _counts(plan: SyncPlan, execution: dict[str, int]) -> dict[str, int]:
    counts = plan.counts()
    counts.update({k: v for k, v in execution.items() if k.startswith("destination_rows")})
    return counts


def _record_failure(repo, run_id, batch_id, committed, error, counts, log, details=None) -> None:
    try:
        repo.fail_sync_run(
            run_id, batch_id,
            status="unverified" if committed else "failed",
            batch_status="sync_unverified" if committed else "sync_failed",
            error=error, counts=counts, details=details,
        )
    except Exception as exc:  # the local database may be the thing that failed
        log.error("could not record the sync failure locally: %s", exc)
