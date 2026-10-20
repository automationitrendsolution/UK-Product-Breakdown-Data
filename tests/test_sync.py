"""Power BI sync with an in-memory destination and staging repository (no real DB)."""
from __future__ import annotations

import copy
import datetime as dt
import logging
from decimal import Decimal

import pytest

from app import db, schema
from app.sync import (
    Destination, SyncFailed, SyncRefused, SyncUnverified, format_text_number, plan_sync, run_sync,
    to_destination, values_equal,
)
from tests.conftest import prod_column_types

D = dt.date(2026, 10, 12)
BATCH_ID = "11111111-2222-3333-4444-555555555555"
FILE = "export.xlsx"
PRESENT = [c.name for c in schema.ALL_COLUMNS if c.name not in ("Expenses",)]


def staged(staging_id, sku, **values):
    row = {c.name: None for c in schema.ALL_COLUMNS}
    row.update({"staging_id": staging_id, "source_file": FILE, "extra_columns": None,
                "Date": D, "Marketplace": "Amazon.co.uk", "ASIN": "B0C787BTZ1", "SKU": sku,
                "SalesOrganic": Decimal("142.4200"), "UnitsOrganic": 8, "Sessions": 26,
                "GrossProfit": Decimal("14.5800"), "NetProfit": Decimal("14.5800"), "ROI": Decimal("29.5700")})
    row.update(values)
    return row


def dest_row(row_id, sku, **values):
    row = {name: None for name in schema.DESTINATION_COLUMN_MAP.values()}
    row.update({"id": row_id, "Date": dt.datetime.combine(D, dt.time()), "Marketplace": "Amazon.co.uk",
                "ASIN": "B0C787BTZ1", "SKU": sku, "SalesOrganic": 142.42, "UnitsOrganic": 8, "Sessions": "26",
                "GrossProfit": 14.58, "NetProfit": 14.58, "Refunds": ""})
    row.update(values)
    return row


class FakeDestination(Destination):
    """In-memory Uk_breakdown_data with transactions; can fail or misbehave on demand."""

    def __init__(self, rows=(), *, fail_on_insert=False, corrupt_writes=False, columns=None):
        self.rows = [copy.deepcopy(r) for r in rows]
        self.types = columns or prod_column_types()
        self.fail_on_insert = fail_on_insert
        self.corrupt_writes = corrupt_writes
        self.read_only = None
        self.writes = 0
        self._snapshot = None
        self.locked = False

    def columns(self):
        return dict(self.types)

    def _in_range(self, row, start, end):
        return start <= row["Date"].date() <= end

    def fetch_rows(self, start, end, columns):
        return [{c: r.get(c) for c in ("id", *columns)} for r in self.rows if self._in_range(r, start, end)]

    def count_rows(self, start, end):
        return sum(1 for r in self.rows if self._in_range(r, start, end))

    def acquire_lock(self):
        self.locked = True
        return True

    def release_lock(self):
        self.locked = False

    def begin(self):
        self._snapshot = copy.deepcopy(self.rows)

    def commit(self):
        self._snapshot = None

    def rollback(self):
        if self._snapshot is not None:
            self.rows = self._snapshot
            self._snapshot = None

    def insert_rows(self, columns, rows):
        assert not self.read_only, "write attempted on a read-only session"
        next_id = max((r["id"] for r in self.rows), default=0) + 1
        for values in rows:
            row = {name: None for name in self.types}
            row.update(dict(zip(columns, values)))
            if self.corrupt_writes:
                row["SalesOrganic"] = 0.0
            row["id"] = next_id
            next_id += 1
            self.rows.append(row)
            self.writes += 1
        if self.fail_on_insert:
            raise RuntimeError("connection lost during insert")
        return len(rows)

    def update_row(self, row_id, key_values, assignments):
        assert not self.read_only, "write attempted on a read-only session"
        matched = [r for r in self.rows if r["id"] == row_id and all(r[k] == v for k, v in key_values.items())]
        for row in matched:
            row.update(assignments)
            self.writes += 1
        return len(matched)

    def close(self):
        pass


class FakeRepo:
    def __init__(self, rows, *, validation_status="passed"):
        self.rows = rows
        self.batch = {"batch_id": BATCH_ID, "status": "imported", "validation_status": validation_status,
                      "start_date": dt.date(2026, 10, 12), "end_date": dt.date(2026, 10, 15),
                      "marketplaces": ["Amazon.co.uk", "Amazon.de"], "rows_valid": len(rows), "rows_rejected": 0,
                      "sync_status": "not_synced"}
        self.runs = []
        self.outcomes = {}

    def get_batch(self, batch_id):
        return dict(self.batch) if batch_id == BATCH_ID else None

    def load_valid_rows(self, batch_id):
        return copy.deepcopy(self.rows)

    def file_columns(self, batch_id):
        return {FILE: PRESENT}

    def start_sync_run(self, batch_id, *, update_existing, destination):
        self.runs.append({"status": "running"})
        return len(self.runs)

    def fail_sync_run(self, run_id, batch_id, *, status, batch_status, error, counts=None, details=None):
        self.runs[run_id - 1].update(status=status, error=error)
        self.batch["sync_status"] = batch_status

    def complete_sync_run(self, run_id, batch_id, *, outcomes, counts, batch_status, details):
        self.runs[run_id - 1].update(status="succeeded", counts=counts)
        self.outcomes = outcomes
        self.batch["sync_status"] = batch_status


def sync(repo, dest, *, dry_run=False, update_existing=False, log=None):
    def open_destination(read_only):
        dest.read_only = read_only
        return dest

    return run_sync(repo, open_destination, BATCH_ID, dry_run=dry_run, update_existing=update_existing,
                    destination_label="fake.Uk_breakdown_data", log=log or logging.getLogger("t"))


# ----------------------------------------------------------------- conversion and comparison


def test_text_columns_get_the_existing_number_format():
    assert to_destination(None, "text") == ""
    assert to_destination(Decimal("2.0000"), "text") == "2"
    assert to_destination(Decimal("-2.7400"), "text") == "-2.74"
    assert to_destination(Decimal("100.0000"), "text") == "100"
    assert to_destination(26, "text") == "26"
    assert format_text_number(Decimal("-0.0000")) == "0"


def test_other_destination_types():
    assert to_destination(D, "datetime") == dt.datetime(2026, 10, 12)
    assert to_destination(None, "float") is None
    assert to_destination(Decimal("8"), "int") == 8
    with pytest.raises(ValueError):
        to_destination(Decimal("1.5"), "int")


def test_values_equal_treats_blank_null_and_zero_alike_and_tolerates_float_storage():
    assert values_equal(None, "") and values_equal(None, 0) and values_equal("", Decimal("0.0000"))
    assert values_equal(Decimal("14.5800"), 14.58)
    assert values_equal(Decimal("142.4200"), 142.4199981689453)  # FLOAT storage of 142.42
    assert values_equal(26, "26") and values_equal("-2.74", Decimal("-2.7400"))
    assert not values_equal(Decimal("14.5800"), 14.59)
    assert not values_equal(26, "")


# ----------------------------------------------------------------- planning


def test_plan_classifies_rows():
    repo_rows = [staged(1, "NEW"), staged(2, "SAME"), staged(3, "CHANGED"), staged(4, "TWICE")]
    existing = [dest_row(10, "SAME"), dest_row(11, "CHANGED", GrossProfit=99.0),
                dest_row(12, "TWICE"), dest_row(13, "twice")]
    plan = plan_sync(FakeRepo(repo_rows).batch, repo_rows, {FILE: PRESENT}, prod_column_types(), existing,
                     update_existing=False)
    assert {a.label.split(" / ")[-1]: a.action for a in plan.actions} == {
        "NEW": "insert", "SAME": "unchanged", "CHANGED": "conflict", "TWICE": "rejected"}
    conflict = plan.of("conflict")[0]
    assert [d[0] for d in conflict.differences] == ["GrossProfit"]
    assert "ROI" in plan.unmapped_values  # staged, but Power BI has no ROI column


def test_plan_refuses_when_a_mapped_column_disappeared():
    types = prod_column_types()
    del types["SB"]
    rows = [staged(1, "A")]
    with pytest.raises(SyncRefused, match="SB"):
        plan_sync(FakeRepo(rows).batch, rows, {FILE: PRESENT}, types, [], update_existing=False)


# ----------------------------------------------------------------- run_sync


def test_dry_run_writes_nothing_and_uses_a_read_only_session():
    dest = FakeDestination([dest_row(10, "SAME")])
    repo = FakeRepo([staged(1, "NEW"), staged(2, "SAME")])
    outcome = sync(repo, dest, dry_run=True)
    assert outcome.status == "planned" and outcome.plan.counts()["insert"] == 1
    assert dest.read_only is True and dest.writes == 0 and len(dest.rows) == 1
    assert repo.runs == []  # nothing recorded locally either


def test_sync_inserts_new_rows_and_preserves_existing_records():
    other_day = dest_row(5, "OLD", Date=dt.datetime(2026, 10, 1))
    same_day_other_sku = dest_row(6, "UNRELATED", SalesOrganic=1.0)
    conflict = dest_row(7, "CHANGED", GrossProfit=99.0)
    dest = FakeDestination([other_day, same_day_other_sku, conflict])
    before = copy.deepcopy(dest.rows)
    repo = FakeRepo([staged(1, "NEW"), staged(2, "CHANGED")])

    outcome = sync(repo, dest)

    assert outcome.execution["inserted"] == 1 and outcome.execution["updated"] == 0
    assert dest.rows[:3] == before  # every pre-existing row untouched, conflict included
    new = dest.rows[3]
    assert (new["SKU"], new["Sessions"], new["Refunds"], new["SalesOrganic"]) == ("NEW", "26", "", Decimal("142.4200"))
    assert outcome.status == "synced_with_conflicts"
    assert repo.outcomes == {1: "inserted", 2: "conflict"}
    assert repo.batch["sync_status"] == "synced_with_conflicts"


def test_rerun_does_not_insert_duplicates():
    dest = FakeDestination()
    repo = FakeRepo([staged(1, "A"), staged(2, "B")])
    assert sync(repo, dest).execution["inserted"] == 2
    second = sync(repo, dest)
    assert second.execution["inserted"] == 0 and second.plan.counts()["unchanged"] == 2
    assert len(dest.rows) == 2
    assert second.status == "synced"


def test_existing_rows_are_updated_only_with_the_explicit_flag():
    dest = FakeDestination([dest_row(7, "CHANGED", GrossProfit=99.0)])
    repo = FakeRepo([staged(1, "CHANGED")])
    sync(repo, dest)
    assert dest.rows[0]["GrossProfit"] == 99.0
    outcome = sync(repo, dest, update_existing=True)
    assert outcome.execution["updated"] == 1 and dest.rows[0]["GrossProfit"] == Decimal("14.5800")
    assert len(dest.rows) == 1 and dest.rows[0]["id"] == 7


def test_destination_unavailable_keeps_staging_and_records_the_failure():
    repo = FakeRepo([staged(1, "A")])
    staged_before = copy.deepcopy(repo.rows)

    def unreachable(read_only):
        raise db.DatabaseUnavailable("cannot connect to the Power BI database")

    with pytest.raises(db.DatabaseUnavailable):
        run_sync(repo, unreachable, BATCH_ID, dry_run=False, update_existing=False,
                 destination_label="x", log=logging.getLogger("t"))
    assert repo.rows == staged_before
    assert repo.runs[-1]["status"] == "failed" and repo.batch["sync_status"] == "sync_failed"


def test_failed_write_rolls_back_and_a_retry_succeeds():
    existing = dest_row(5, "OLD")
    dest = FakeDestination([existing], fail_on_insert=True)
    repo = FakeRepo([staged(1, "A"), staged(2, "B")])

    with pytest.raises(RuntimeError, match="connection lost"):
        sync(repo, dest)
    assert [r["id"] for r in dest.rows] == [5], "the partial insert must be rolled back"
    assert repo.batch["sync_status"] == "sync_failed" and not dest.locked

    dest.fail_on_insert = False
    outcome = sync(repo, dest)
    assert outcome.execution["inserted"] == 2
    assert sorted(r["SKU"] for r in dest.rows) == ["A", "B", "OLD"]
    assert repo.batch["sync_status"] == "synced"


def test_success_is_not_reported_when_verification_fails():
    dest = FakeDestination(corrupt_writes=True)  # commits, but the stored values are wrong
    repo = FakeRepo([staged(1, "A")])
    with pytest.raises(SyncUnverified):
        sync(repo, dest)
    assert repo.runs[-1]["status"] == "unverified" and repo.batch["sync_status"] == "sync_unverified"


def test_destination_changed_between_plan_and_write_is_refused():
    dest = FakeDestination()
    repo = FakeRepo([staged(1, "A")])
    real_begin = dest.begin

    def begin_after_someone_else_inserted():
        real_begin()
        dest.rows.append(dest_row(99, "A"))

    dest.begin = begin_after_someone_else_inserted
    with pytest.raises(SyncFailed, match="changed after the sync was planned"):
        sync(repo, dest)
    assert [r["id"] for r in dest.rows] == []  # rolled back to the state at begin()


def test_batch_that_failed_validation_is_refused():
    repo = FakeRepo([staged(1, "A")], validation_status="failed")
    with pytest.raises(SyncRefused, match="failed validation"):
        sync(repo, FakeDestination())


def test_rows_outside_the_batch_scope_are_refused():
    repo = FakeRepo([staged(1, "A", Date=dt.date(2026, 10, 20))])
    with pytest.raises(SyncRefused, match="outside the batch range"):
        sync(repo, FakeDestination())
    repo = FakeRepo([staged(1, "A", Marketplace="Amazon.com")])
    with pytest.raises(SyncRefused, match="not in the batch scope"):
        sync(repo, FakeDestination())
