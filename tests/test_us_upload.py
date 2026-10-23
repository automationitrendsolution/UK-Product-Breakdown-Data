"""run.py --us-premium (app/us_upload.py: replace by date) against a throwaway US-shaped table."""
from __future__ import annotations

import datetime as dt
import os

import pymysql
import pytest

import run
from app import us_upload
from app import schema
from tests.conftest import REAL_HEADERS, sample_row, write_workbook
from tests.test_integration_mysql import US_FLOAT, mysql_root, sandbox  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integration
TABLE = "Sellerboard_breakdown_data_2024"
US_EXPORT = "EcoNour_Dashboard_by_product_12_10_2026-13_10_2026_(2026_10_14_06_00_00_000)_sellerboard.com.xlsx"


@pytest.fixture
def us(sandbox, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(os, "environ", dict(os.environ))  # the profile's settings must not leak to other tests
    columns = ["`Date` datetime NULL"] + [
        f"`{name}` {'float' if name in US_FLOAT else 'text'} NULL"
        for name in schema.DESTINATION_COLUMN_MAP.values() if name != "Date"] + ["`id` int NOT NULL AUTO_INCREMENT PRIMARY KEY"]
    with sandbox.root.cursor() as cur:
        cur.execute(f"CREATE TABLE `{sandbox.database}`.`{TABLE}` ({', '.join(columns)})")
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{sandbox.database}`.`{TABLE}` TO %s@'%%'",
                    (os.environ["POWERBI_DB_USER"],))
    os.environ.update({
        "US_SELLERBOARD_ACCOUNT": "premiumcarproduct@gmail.com", "US_POWERBI_DB_TABLE": TABLE,
        "US_SELLERBOARD_ALLOWED_MARKETPLACES": "Amazon.com,Amazon.ca,Amazon.com.mx,Non-Amazon",
        "US_SELLERBOARD_FILE_PATTERN": "EcoNour_Dashboard_by_product_*.xlsx",
        "US_SELLERBOARD_DATA_DIR": str(tmp_path / "us" / "data"), "US_SELLERBOARD_LOG_DIR": str(tmp_path / "us" / "logs"),
    })
    incoming = tmp_path / "us" / "data" / "incoming"
    incoming.mkdir(parents=True)

    def seed(**values):
        names = ", ".join(f"`{k}`" for k in values)
        with sandbox.root.cursor() as cur:
            cur.execute(f"INSERT INTO `{sandbox.database}`.`{TABLE}` ({names}) VALUES ({', '.join(['%s'] * len(values))})",
                        tuple(values.values()))

    def rows():
        return sandbox.query(f"SELECT * FROM `{sandbox.database}`.`{TABLE}` ORDER BY id")

    return incoming, seed, rows


def _seed_existing(seed):
    seed(Date=dt.datetime(2026, 10, 12), Marketplace="Amazon.com", ASIN="B0C787BTZ1", SKU="OLD", SalesOrganic=1.0)
    seed(Date=dt.datetime(2026, 10, 12), Marketplace="Amazon.ca", ASIN="B0C787BTZ1", SKU="CA-KEEP", SalesOrganic=2.0)
    seed(Date=dt.datetime(2026, 10, 11), Marketplace="Amazon.com", ASIN="B0C787BTZ1", SKU="DAY-KEEP", SalesOrganic=3.0)


def test_replaces_only_the_files_dates_and_marketplaces(us):
    incoming, seed, rows = us
    _seed_existing(seed)
    today = dt.date.today().strftime("%d/%m/%Y")
    write_workbook(incoming / US_EXPORT, [
        sample_row(Marketplace="Amazon.com", SKU="NEW-1"),
        sample_row(Marketplace="Amazon.com", SKU="NEW-2"),
        sample_row(Marketplace="Amazon.com", SKU="TODAY", Date=today),        # incomplete day: left out
        sample_row(Marketplace="Amazon.co.uk", SKU="UK"),                      # not a US marketplace: left out
    ])
    assert run.main(["--us-premium", "--no-fetch", "--dry-run"]) == 0
    assert [r["SKU"] for r in rows()] == ["OLD", "CA-KEEP", "DAY-KEEP"] and (incoming / US_EXPORT).exists()

    assert run.main(["--us-premium", "--no-fetch"]) == 0
    after = {r["SKU"]: r for r in rows()}
    assert sorted(after) == ["CA-KEEP", "DAY-KEEP", "NEW-1", "NEW-2"]   # OLD replaced; .ca and 11 Oct untouched
    new = after["NEW-1"]
    assert (new["Date"], new["Marketplace"], new["UnitsOrganic"], new["Sessions"], new["VAT"]) == (
        dt.datetime(2026, 10, 12), "Amazon.com", 8.0, 26.0, "-42.5")
    assert abs(new["SB"] + 0.52) < 1e-6 and abs(new["Commission"] + 38.25) < 1e-6
    assert not (incoming / US_EXPORT).exists()
    assert (incoming.parent / "processed" / US_EXPORT).exists()            # the Excel is kept


def test_summary_export_is_refused_and_nothing_changes(us):
    incoming, seed, rows = us
    _seed_existing(seed)
    before = rows()
    headers = [h for h in REAL_HEADERS if h not in ("Commission", "ProductCost Sales")] + ["AmazonFees", "Cost of Goods"]
    write_workbook(incoming / US_EXPORT, [sample_row(Marketplace="Amazon.com", SKU="X")], headers)
    assert run.main(["--us-premium", "--no-fetch"]) == 7
    assert rows() == before
    assert (incoming.parent / "failed" / US_EXPORT).exists()


def test_failure_halfway_rolls_everything_back(us, monkeypatch):
    incoming, seed, rows = us
    _seed_existing(seed)
    before = rows()
    write_workbook(incoming / US_EXPORT, [sample_row(Marketplace="Amazon.com", SKU=f"N{i}") for i in range(3)])
    monkeypatch.setattr(us_upload, "INSERT_CHUNK", 1)
    real = pymysql.cursors.Cursor.executemany
    calls = {"n": 0}

    def fail_second_chunk(self, query, args):
        calls["n"] += 1
        if calls["n"] == 2:
            raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")
        return real(self, query, args)

    monkeypatch.setattr(pymysql.cursors.Cursor, "executemany", fail_second_chunk)
    assert run.main(["--us-premium", "--no-fetch"]) == 9
    assert rows() == before, "the delete and the first chunk must be rolled back"
    assert (incoming / US_EXPORT).exists(), "the Excel stays for the next run"


def test_us_premium_refuses_uk_only_options(us):
    assert run.main(["--us-premium", "--preview"]) == 2
    assert run.main(["--us-premium", "--local-only", "--no-fetch"]) == 2
