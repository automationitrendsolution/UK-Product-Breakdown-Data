"""End-to-end tests of run.py against the sellerboard-mysql container.

Each test gets a throwaway database (sbtest_<random>) holding a production-shaped
Uk_breakdown_data, plus two throwaway users: a "local" user with full access to that
database and a "Power BI" user limited to SELECT/INSERT/UPDATE on Uk_breakdown_data.
Everything is dropped afterwards; the real sellerboard database is not touched.
Skipped when the container is not reachable.
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
import shutil
from dataclasses import dataclass

import pymysql
import pytest

import run
from app import config, staging
from app.sync import MySQLDestination
from tests.conftest import EXPORT_NAME, REAL_HEADERS, prod_table_ddl, sample_row, write_workbook

pytestmark = pytest.mark.integration
SCOPE = ["--start-date", "2026-10-12", "--end-date", "2026-10-15"]


@dataclass
class Sandbox:
    root: pymysql.connections.Connection
    database: str

    def query(self, sql, params=()):
        with self.root.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def dest_rows(self):
        return self.query(f"SELECT * FROM `{self.database}`.`Uk_breakdown_data` ORDER BY id")

    def seed(self, **values):
        columns = ", ".join(f"`{c}`" for c in values)
        placeholders = ", ".join(["%s"] * len(values))
        with self.root.cursor() as cur:
            cur.execute(f"INSERT INTO `{self.database}`.`Uk_breakdown_data` ({columns}) VALUES ({placeholders})",
                        tuple(values.values()))

    def batches(self):
        return self.query(f"SELECT * FROM `{self.database}`.import_batches ORDER BY created_at")

    def staging_status(self):
        rows = self.query(f"SELECT sync_status, COUNT(*) AS n FROM `{self.database}`.uk_breakdown_staging "
                          "WHERE validation_status = 'valid' GROUP BY sync_status")
        return {r["sync_status"]: r["n"] for r in rows}


@pytest.fixture(scope="module")
def mysql_root():
    config.load_env()
    password = os.environ.get("SELLERBOARD_DB_ROOT_PASSWORD")
    if not password:
        pytest.skip("SELLERBOARD_DB_ROOT_PASSWORD is not set")
    host = os.environ.get("SELLERBOARD_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("SELLERBOARD_DB_PORT", "3310"))
    try:
        conn = pymysql.connect(host=host, port=port, user="root", password=password, autocommit=True,
                               connect_timeout=3, charset="utf8mb4")
    except pymysql.err.MySQLError as exc:
        pytest.skip(f"sellerboard-mysql is not reachable: {exc}")
    yield conn, host, port
    conn.close()


@pytest.fixture
def sandbox(mysql_root, monkeypatch):
    root, host, port = mysql_root
    suffix = secrets.token_hex(4)
    database, local_user, powerbi_user = f"sbtest_{suffix}", f"sbt_local_{suffix}", f"sbt_pbi_{suffix}"
    local_password, powerbi_password = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    with root.cursor() as cur:
        cur.execute(f"CREATE DATABASE `{database}`")
        cur.execute(f"USE `{database}`")
        cur.execute(prod_table_ddl())
        cur.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (local_user, local_password))
        cur.execute(f"GRANT ALL ON `{database}`.* TO %s@'%%'", (local_user,))
        cur.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (powerbi_user, powerbi_password))
        cur.execute(f"GRANT SELECT, INSERT, UPDATE ON `{database}`.`Uk_breakdown_data` TO %s@'%%'", (powerbi_user,))
    for prefix, user, password in (("SELLERBOARD_DB_", local_user, local_password),
                                   ("POWERBI_DB_", powerbi_user, powerbi_password)):
        for key, value in {"HOST": host, "PORT": str(port), "NAME": database, "USER": user, "PASSWORD": password}.items():
            monkeypatch.setenv(prefix + key, value)
    monkeypatch.setenv("POWERBI_DB_TABLE", "Uk_breakdown_data")
    monkeypatch.delenv("SELLERBOARD_ALLOWED_MARKETPLACES", raising=False)
    try:
        yield Sandbox(root, database)
    finally:
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{database}`")
            cur.execute("DROP USER IF EXISTS %s@'%%', %s@'%%'", (local_user, powerbi_user))


def test_end_to_end_incoming_staging_and_power_bi(sandbox, project_dirs, tmp_path):
    db_name = sandbox.database
    incoming, processed = project_dirs / "incoming", project_dirs / "processed"
    incoming.mkdir(parents=True)
    sandbox.seed(Date=dt.datetime(2026, 10, 1), Marketplace="Amazon.co.uk", ASIN="B0OLDOLD01", SKU="OLD",
                 SalesOrganic=5.0, Sessions="3", Refunds="")
    sandbox.seed(Date=dt.datetime(2026, 10, 13), Marketplace="Amazon.co.uk", ASIN="B0C787BTZ1", SKU="EcoDogBag",
                 GrossProfit=14.58, NetProfit=14.58, Sessions="")

    # 1. One file imported in place with --file, and synced in the same run; it is not moved.
    first = write_workbook(tmp_path / EXPORT_NAME, [sample_row(SKU="EcoDogBag")])
    assert run.main(["--file", str(first), *SCOPE]) == 0
    assert first.exists() and len(sandbox.dest_rows()) == 3

    # 2. The full export arrives in incoming/.
    rows = [
        sample_row(SKU="EcoDogBag"),                                        # already in Power BI, same values
        sample_row(SKU="EcoDogBag-FBM", SalesOrganic=10.0),                 # same ASIN, new SKU -> insert
        sample_row(Date="13/10/2026", SKU="EcoDogBag", GrossProfit=20.0, NetProfit=20.0),  # differs -> conflict
        sample_row(Date="13/10/2026", Marketplace="Amazon.de", SKU="DE-1"),  # new -> insert
        sample_row(Date="13/10/2026", Marketplace="Amazon.com", SKU="US-1"),  # not a UK-table marketplace
        sample_row(Date="16/10/2026", SKU="LATE"),                          # after the end date
    ]
    write_workbook(incoming / EXPORT_NAME, rows)
    assert run.main(["--no-fetch", "--dry-run", *SCOPE]) == 0
    assert len(sandbox.batches()) == 1 and (incoming / EXPORT_NAME).exists()  # a dry run writes and moves nothing

    assert run.main(["--no-fetch", "--no-sync", *SCOPE]) == 0
    assert not (incoming / EXPORT_NAME).exists() and (processed / EXPORT_NAME).exists()
    shutil.copy2(processed / EXPORT_NAME, incoming / EXPORT_NAME)
    assert run.main(["--no-fetch", "--no-sync", *SCOPE]) == 0  # the same file again: recognised, not staged twice
    batches = sandbox.batches()
    assert len(batches) == 2
    batch = batches[-1]
    assert (batch["rows_read"], batch["rows_valid"], batch["rows_excluded_date"],
            batch["rows_excluded_marketplace"], batch["validation_status"]) == (6, 4, 1, 1, "passed")
    staged = sandbox.query(f"SELECT `SB`, `UnitsOrganic`, `ROI` FROM `{db_name}`.uk_breakdown_staging "
                           "WHERE batch_id = %s AND `SKU` = 'DE-1'", (batch["batch_id"],))[0]
    assert (str(staged["SB"]), staged["UnitsOrganic"], str(staged["ROI"])) == ("-0.5200", 8, "29.5700")

    # 3. Preview: Power BI is only read.
    before = sandbox.dest_rows()
    assert run.main(["--preview"]) == 0
    assert sandbox.dest_rows() == before

    # 4. The sync: two inserts; the identical row and the conflicting row are left as they are.
    assert run.main(["--no-fetch"]) == 0
    after = sandbox.dest_rows()
    assert after[:len(before)] == before, "pre-existing Power BI rows must be untouched"
    new = {r["SKU"]: r for r in after[len(before):]}
    assert sorted(new) == ["DE-1", "EcoDogBag-FBM"]
    de = new["DE-1"]
    assert de["Date"] == dt.datetime(2026, 10, 13) and de["Marketplace"] == "Amazon.de"
    assert (de["Sessions"], de["SB"], de["Refunds"], de["SalesSponsoredDisplay"]) == ("26", "-0.52", "2", "")
    assert de["Refund Goodwill"] is None  # not in this export: left NULL, as the existing loader does
    assert de["UnitsOrganic"] == 8 and abs(de["SalesOrganic"] - 142.42) < 0.005
    assert sandbox.query(f"SELECT sync_status FROM `{db_name}`.import_batches WHERE batch_id = %s",
                         (batch["batch_id"],))[0]["sync_status"] == "synced_with_conflicts"

    # 5. Nothing is waiting any more: another run changes nothing.
    assert run.main(["--no-fetch"]) == 0
    assert sandbox.dest_rows() == after
    runs = sandbox.query(f"SELECT status, rows_inserted, rows_updated FROM `{db_name}`.sync_runs ORDER BY run_id")
    assert [(r["status"], r["rows_inserted"], r["rows_updated"]) for r in runs] == [
        ("succeeded", 1, 0), ("succeeded", 2, 0)]
    dupes = sandbox.query(f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM `{db_name}`.Uk_breakdown_data "
                          "GROUP BY `Date`, `Marketplace`, `ASIN`, `SKU` HAVING COUNT(*) > 1) d")
    assert dupes[0]["n"] == 0
    assert (project_dirs.parent / "logs" / "cron.log").read_text(encoding="utf-8").count("exit=0") == 7  # one line per run


def test_dry_run_creates_nothing(sandbox, tmp_path):
    path = write_workbook(tmp_path / EXPORT_NAME, [sample_row()])
    assert run.main(["--file", str(path), "--dry-run", *SCOPE]) == 0
    tables = sandbox.query(f"SHOW TABLES FROM `{sandbox.database}`")
    assert [list(t.values())[0] for t in tables] == ["Uk_breakdown_data"]


def test_import_rolls_back_on_failure_and_retry_succeeds(sandbox, tmp_path, monkeypatch):
    path = write_workbook(tmp_path / EXPORT_NAME, [sample_row(SKU=f"S{i}") for i in range(5)])
    real_params = staging._staging_params
    calls = {"n": 0}

    def fail_on_fourth_row(batch_id, row):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("simulated failure in the middle of the import")
        return real_params(batch_id, row)

    monkeypatch.setattr(staging, "INSERT_CHUNK", 2)  # rows 1-2 are already inserted when row 4 fails
    monkeypatch.setattr(staging, "_staging_params", fail_on_fourth_row)
    assert run.main(["--file", str(path), "--no-sync", *SCOPE]) == 1
    count = sandbox.query(f"SELECT COUNT(*) AS n FROM `{sandbox.database}`.uk_breakdown_staging")[0]["n"]
    assert count == 0
    (failed,) = sandbox.batches()
    assert failed["status"] == "failed" and "simulated failure" in failed["error_message"]

    monkeypatch.setattr(staging, "_staging_params", real_params)
    assert run.main(["--file", str(path), "--no-sync", *SCOPE]) == 0
    count = sandbox.query(f"SELECT COUNT(*) AS n FROM `{sandbox.database}`.uk_breakdown_staging")[0]["n"]
    assert count == 5


def test_sync_failure_rolls_back_power_bi_and_the_next_run_retries(sandbox, tmp_path, monkeypatch):
    sandbox.seed(Date=dt.datetime(2026, 10, 12), Marketplace="Amazon.co.uk", ASIN="B0OLDOLD01", SKU="OLD")
    path = write_workbook(tmp_path / EXPORT_NAME, [sample_row(SKU="A"), sample_row(SKU="B")])
    before = sandbox.dest_rows()
    real_count = MySQLDestination.count_rows
    monkeypatch.setattr(MySQLDestination, "count_rows", lambda self, s, e: real_count(self, s, e) + 100)
    assert run.main(["--file", str(path), *SCOPE]) == 9
    assert sandbox.dest_rows() == before, "the inserts must have been rolled back"
    assert sandbox.staging_status() == {"pending": 2}
    run_row = sandbox.query(f"SELECT status, error_message FROM `{sandbox.database}`.sync_runs")[0]
    assert run_row["status"] == "failed" and "row count check failed" in run_row["error_message"]

    monkeypatch.setattr(MySQLDestination, "count_rows", real_count)
    assert run.main(["--no-fetch"]) == 0  # the next run picks up the waiting batch
    assert sorted(r["SKU"] for r in sandbox.dest_rows()) == ["A", "B", "OLD"]
    assert sandbox.staging_status() == {"inserted": 2}


def test_power_bi_unavailable_keeps_staging_for_the_next_run(sandbox, tmp_path, monkeypatch):
    path = write_workbook(tmp_path / EXPORT_NAME, [sample_row()])
    good_port = os.environ["POWERBI_DB_PORT"]
    monkeypatch.setenv("POWERBI_DB_PORT", "1")
    assert run.main(["--file", str(path), *SCOPE]) == 9
    assert sandbox.staging_status() == {"pending": 1}
    assert sandbox.batches()[0]["sync_status"] == "sync_failed"

    monkeypatch.setenv("POWERBI_DB_PORT", good_port)
    assert run.main(["--no-fetch"]) == 0
    assert len(sandbox.dest_rows()) == 1


def test_file_with_rejected_rows_is_staged_but_never_synced(sandbox, project_dirs):
    incoming = project_dirs / "incoming"
    incoming.mkdir(parents=True)
    write_workbook(incoming / EXPORT_NAME, [sample_row(SKU="A"), sample_row(SKU="B", UnitsOrganic=1.5)])
    assert run.main(["--no-fetch", *SCOPE]) == 7
    assert (project_dirs / "failed" / EXPORT_NAME).exists()
    rejected = sandbox.query(f"SELECT validation_errors, raw_row FROM `{sandbox.database}`.uk_breakdown_staging "
                             "WHERE validation_status = 'rejected'")
    assert len(rejected) == 1 and "UnitsOrganic" in rejected[0]["validation_errors"] and rejected[0]["raw_row"]
    assert sandbox.dest_rows() == []


def test_local_only_batch_is_never_sent_to_power_bi(sandbox, tmp_path):
    rows = [sample_row(SKU="A"), sample_row(SKU="B")]
    for row in rows:  # a summary export: totals instead of the itemised fee and cost columns
        row.update({"AmazonFees": -90.95, "RefundCost": -17.98, "Cost of Goods": -49.3})
    headers = [h for h in REAL_HEADERS if h not in ("Commission", "ProductCost Sales")] + [
        "AmazonFees", "RefundCost", "Cost of Goods"]
    path = write_workbook(tmp_path / EXPORT_NAME, rows, headers)
    assert run.main(["--file", str(path), "--local-only", *SCOPE]) == 0
    assert run.main(["--no-fetch"]) == 0  # a later normal run must not pick it up
    assert sandbox.dest_rows() == []
    (batch,) = sandbox.batches()
    assert batch["sync_status"] == "local_only"
    stored = sandbox.query(f"SELECT `AmazonFees`, `RefundCost`, `Cost of Goods` FROM "
                           f"`{sandbox.database}`.uk_breakdown_staging WHERE `SKU` = 'A'")[0]
    assert [str(v) for v in stored.values()] == ["-90.9500", "-17.9800", "-49.3000"]
