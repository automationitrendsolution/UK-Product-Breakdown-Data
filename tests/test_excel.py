"""Excel parsing, column mapping, date and marketplace filtering, duplicate detection."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app import config, excel, schema
from tests.conftest import EXPORT_NAME, REAL_HEADERS, sample_row, write_workbook

D = dt.date
UK_ONLY = ("Amazon.co.uk",)


def plan_for(tmp_path, rows, start=D(2026, 10, 12), end=D(2026, 10, 15), marketplaces=config.DEFAULT_MARKETPLACES,
             headers=None, name=EXPORT_NAME):
    path = write_workbook(tmp_path / name, rows, headers)
    return excel.build_plan([path], start, end, marketplaces, tmp_path, "*.xlsx")


def test_every_real_header_is_mapped_and_sb_is_renamed():
    lookup = schema.header_lookup()
    assert [h for h in REAL_HEADERS if h not in lookup] == []
    assert lookup["SponsoredВrands"] == "SB"  # Cyrillic В in the export
    assert lookup["SponsoredBrands"] == "SB"


def test_parse_maps_columns_and_types(tmp_path):
    plan = plan_for(tmp_path, [sample_row()])
    (row,) = plan.rows
    assert row.status == "valid"
    assert row.values["Date"] == D(2026, 10, 12)  # 12/10/2026 is day-first
    assert row.values["SB"] == Decimal("-0.5200")
    assert row.values["SalesOrganic"] == Decimal("142.4200")
    assert row.values["UnitsOrganic"] == 8 and isinstance(row.values["UnitsOrganic"], int)
    assert row.values["Sessions"] == 26
    assert row.values["ROI"] == Decimal("29.5700")
    assert row.values["Name"].startswith("EcoNour")
    assert row.values["Fulfillment Channel"] == "FBA"
    assert row.values["PromoValue"] is None
    assert plan.passed


def test_unknown_columns_are_kept_not_dropped(tmp_path):
    rows = [dict(sample_row(), **{"Brand New Fee": -1.25})]
    plan = plan_for(tmp_path, rows, headers=REAL_HEADERS + ["Brand New Fee"])
    (row,) = plan.rows
    assert row.extra == {"Brand New Fee": -1.25}
    assert plan.files[0].unknown_headers == ["Brand New Fee"]
    assert any("Brand New Fee" in w for w in plan.warnings)


def test_date_filter_is_inclusive(tmp_path):
    rows = [sample_row(Date=d, SKU=f"SKU{i}") for i, d in
            enumerate(["11/10/2026", "12/10/2026", "15/10/2026", "16/10/2026"])]
    plan = plan_for(tmp_path, rows)
    assert sorted(r.values["Date"] for r in plan.rows) == [D(2026, 10, 12), D(2026, 10, 15)]
    assert plan.files[0].excluded_by_date == 2
    assert (plan.files[0].data_min_date, plan.files[0].data_max_date) == (D(2026, 10, 11), D(2026, 10, 16))


def test_single_day_range(tmp_path):
    rows = [sample_row(Date=d, SKU=f"SKU{i}") for i, d in enumerate(["12/10/2026", "13/10/2026"])]
    plan = plan_for(tmp_path, rows, start=D(2026, 10, 13), end=D(2026, 10, 13))
    assert [r.values["Date"] for r in plan.rows] == [D(2026, 10, 13)]


def test_iso_and_real_date_cells_are_accepted(tmp_path):
    rows = [sample_row(Date="2026-10-13", SKU="A"), sample_row(Date=dt.datetime(2026, 10, 14), SKU="B")]
    plan = plan_for(tmp_path, rows)
    assert sorted(r.values["Date"] for r in plan.rows) == [D(2026, 10, 13), D(2026, 10, 14)]


def test_uk_marketplace_filter(tmp_path):
    rows = [
        sample_row(Marketplace="Amazon.co.uk", SKU="A"),
        sample_row(Marketplace="Amazon.de", SKU="B"),
        sample_row(Marketplace="Amazon.com", SKU="C"),
        sample_row(Marketplace=None, SKU="D"),
    ]
    plan = plan_for(tmp_path, rows, marketplaces=UK_ONLY)
    assert [r.values["Marketplace"] for r in plan.rows] == ["Amazon.co.uk"]
    assert plan.files[0].excluded_marketplaces == {"Amazon.de": 1, "Amazon.com": 1, "(blank)": 1}


def test_default_scope_keeps_european_marketplaces_but_not_us(tmp_path):
    rows = [sample_row(Marketplace=m, SKU=m) for m in ("Amazon.co.uk", "Amazon.de", "Amazon.it", "Amazon.com")]
    plan = plan_for(tmp_path, rows)
    assert sorted(r.values["Marketplace"] for r in plan.rows) == ["Amazon.co.uk", "Amazon.de", "Amazon.it"]
    assert plan.files[0].excluded_marketplaces == {"Amazon.com": 1}


def test_same_asin_different_sku_is_not_a_duplicate(tmp_path):
    plan = plan_for(tmp_path, [sample_row(SKU="EcoDogBag"), sample_row(SKU="EcoDogBag-FBM")])
    assert [r.status for r in plan.rows] == ["valid", "valid"]


def test_identical_duplicate_rows_keep_one(tmp_path):
    plan = plan_for(tmp_path, [sample_row(), sample_row()])
    assert [r.status for r in plan.rows] == ["valid", "duplicate"]
    assert "identical to" in plan.rows[1].errors[0]
    assert plan.passed


def test_conflicting_duplicate_rows_are_all_rejected(tmp_path):
    plan = plan_for(tmp_path, [sample_row(), sample_row(SalesOrganic=99.0)])
    assert [r.status for r in plan.rows] == ["rejected", "rejected"]
    assert not plan.passed


def test_duplicates_across_two_files(tmp_path):
    first = write_workbook(tmp_path / "Econour_a.xlsx", [sample_row()])
    second = write_workbook(tmp_path / "Econour_b.xlsx", [sample_row(), sample_row(SKU="Other")])
    plan = excel.build_plan([first, second], D(2026, 10, 12), D(2026, 10, 15), UK_ONLY, tmp_path, "*.xlsx")
    assert [(r.source_file, r.status) for r in plan.rows] == [
        ("Econour_a.xlsx", "valid"), ("Econour_b.xlsx", "duplicate"), ("Econour_b.xlsx", "valid"),
    ]


def test_duplicate_key_is_case_insensitive_like_mysql(tmp_path):
    plan = plan_for(tmp_path, [sample_row(SKU="EcoDogBag"), sample_row(SKU="ecodogbag")])
    assert all(r.status == "rejected" for r in plan.rows)


@pytest.mark.parametrize("override, message", [
    ({"UnitsOrganic": 1.5}, "UnitsOrganic"),
    ({"SalesOrganic": "abc"}, "SalesOrganic"),
    ({"SKU": None}, "SKU is blank"),
    ({"ASIN": "B0C787"}, "not a 10-character ASIN"),
    ({"Date": "2026/31/31"}, "Date"),
])
def test_invalid_rows_are_rejected_with_reason(tmp_path, override, message):
    plan = plan_for(tmp_path, [sample_row(**override), sample_row(SKU="good")])
    bad = next(r for r in plan.rows if r.status == "rejected")
    assert any(message in e for e in bad.errors)
    assert bad.raw and "Marketplace" in bad.raw  # the source cells are kept for inspection
    assert not plan.passed


def test_placeholder_asin_is_accepted(tmp_path):
    plan = plan_for(tmp_path, [sample_row(ASIN="-")])
    assert plan.rows[0].status == "valid"


def test_missing_required_column_refuses_the_file(tmp_path):
    headers = [h for h in REAL_HEADERS if h != "SKU"]
    with pytest.raises(excel.InputError, match="missing column.*SKU"):
        plan_for(tmp_path, [sample_row()], headers=headers)


def test_duplicate_header_refuses_the_file(tmp_path):
    with pytest.raises(excel.InputError, match="more than once"):
        plan_for(tmp_path, [sample_row()], headers=REAL_HEADERS + ["Margin"])


def test_empty_input_directory(tmp_path):
    assert excel.find_input_files(tmp_path, "Econour_*.xlsx") == []
    with pytest.raises(excel.InputError, match="does not exist"):
        excel.find_input_files(tmp_path / "missing", "Econour_*.xlsx")


def test_file_matching_is_case_sensitive_and_skips_excel_lock_files(tmp_path):
    for name in ("Econour_Dashboard_by_product_x.xlsx", "econour_dashboard_by_product_y.xlsx",
                 "~$Econour_Dashboard_by_product_x.xlsx", "Econour_Dashboard_by_product_z.csv"):
        (tmp_path / name).write_bytes(b"")
    found = excel.find_input_files(tmp_path, config.DEFAULT_FILE_PATTERN)
    assert [p.name for p in found] == ["Econour_Dashboard_by_product_x.xlsx"]


def test_swapped_day_month_is_flagged_against_the_file_name(tmp_path):
    plan = plan_for(tmp_path, [sample_row(Date="2026-12-10")], start=D(2026, 12, 1), end=D(2026, 12, 31))
    assert any("check the day/month order" in w for w in plan.warnings)


def test_missing_dates_are_reported(tmp_path):
    plan = plan_for(tmp_path, [sample_row()], start=D(2026, 10, 12), end=D(2026, 10, 13))
    assert plan.missing_dates() == [D(2026, 10, 13)]


def test_fingerprint_depends_on_files_and_scope(tmp_path):
    a = plan_for(tmp_path, [sample_row()])
    b = plan_for(tmp_path, [sample_row()])
    c = plan_for(tmp_path, [sample_row()], end=D(2026, 10, 16))
    assert a.fingerprint() == b.fingerprint() != c.fingerprint()


def test_source_file_is_never_modified_or_deleted(tmp_path):
    path = write_workbook(tmp_path / EXPORT_NAME, [sample_row()])
    before = path.read_bytes()
    excel.build_plan([path], D(2026, 10, 12), D(2026, 10, 15), UK_ONLY, tmp_path, "*.xlsx")
    assert path.exists() and path.read_bytes() == before
