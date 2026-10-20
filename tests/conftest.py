"""Shared fixtures: workbook builder, production-shaped destination schema, logging."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import openpyxl
import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app import schema  # noqa: E402

# The 63 headers of the real export, in order (Econour_Dashboard_by_product_12_10_2026-17_10_2026_...xlsx).
REAL_HEADERS = [
    "Date", "Marketplace", "ASIN", "SKU", "Name", "SalesOrganic", "SalesPPC", "SalesSponsoredProducts",
    "SalesSponsoredDisplay", "UnitsOrganic", "UnitsPPC", "UnitsSponsoredProducts", "UnitsSponsoredDisplay",
    "Refunds", "PromoValue", "SponsoredProducts", "SponsoredDisplay", "SponsoredВrands", "SponsoredBrandsVideo",
    "Google ads", "Facebook ads", "GiftWrap", "Shipping", "Refund Commission", "Refund DigitalServicesFee",
    "Refund GoodwillPrincipal", "Refund Principal", "Refund Promotion", "Refund RefundCommission",
    "Refund ShippingCharge", "Refund ShippingChargeback", "Refund ShippingTax", "Refund ShippingTaxDiscount",
    "Refund ShipPromotion", "Refund TaxDiscount", "RefundsCostsDamaged", "Value of returned items",
    "ProductCost Unsellable Refunds", "Commission", "COMPENSATED_CLAWBACK", "DigitalServicesFee", "FBADisposalFee",
    "FBAPerUnitFulfillmentFee", "REVERSAL_REIMBURSEMENT", "WAREHOUSE_LOST", "EstimatedPayout", "ProductCost Sales",
    "ProductCost Non-Amazon", "ProductCost MultichannelCosts", "ProductCost MissingFromInbound",
    "ProductCost CostOfMissingReturns", "VAT", "GrossProfit", "NetProfit", "Margin", "Real ACOS", "Sessions",
    "Unit Session Percentage", "Ads spend", "Sellable Returns %", "ROI", "Fulfillment Channel", "Expenses",
]

# powerbidb.Uk_breakdown_data column types (information_schema, 2026-10-15).
PROD_FLOAT = {
    "SalesOrganic", "SalesPPC", "SalesSponsoredProducts", "SponsoredProducts", "Commission", "EstimatedPayout",
    "ProductCost Sales", "VAT", "GrossProfit", "NetProfit", "Margin", "Real ACOS",
}
PROD_INT = {"UnitsOrganic", "UnitsPPC", "UnitsSponsoredProducts"}


def prod_column_types() -> dict[str, str]:
    types = {}
    for name in schema.DESTINATION_COLUMN_MAP.values():
        if name == "Date":
            types[name] = "datetime"
        elif name in PROD_FLOAT:
            types[name] = "float"
        elif name in PROD_INT:
            types[name] = "int"
        else:
            types[name] = "text"
    types["id"] = "int"
    return types


def prod_table_ddl(table: str = "Uk_breakdown_data") -> str:
    columns = [
        f"`{name}` {data_type} NULL" for name, data_type in prod_column_types().items() if name != "id"
    ]
    columns.append("`id` int NOT NULL AUTO_INCREMENT PRIMARY KEY")
    return f"CREATE TABLE `{table}` ({', '.join(columns)}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def sample_row(**overrides) -> dict:
    """One export row (values from the real file), keyed by the real headers."""
    row = {
        "Date": "12/10/2026", "Marketplace": "Amazon.co.uk", "ASIN": "B0C787BTZ1", "SKU": "EcoDogBag",
        "Name": "EcoNour Dog Poo Bags 540 Counts", "SalesOrganic": 142.42, "SalesPPC": 112.41,
        "SalesSponsoredProducts": 112.41, "UnitsOrganic": 8.0, "UnitsPPC": 9.0, "UnitsSponsoredProducts": 9.0,
        "Refunds": 2.0, "SponsoredProducts": -55.63, "SponsoredВrands": -0.52, "Shipping": -8.49,
        "Commission": -38.25, "DigitalServicesFee": -1.87, "FBAPerUnitFulfillmentFee": -52.7,
        "EstimatedPayout": 106.38, "ProductCost Sales": -49.3, "VAT": -42.5, "GrossProfit": 14.58,
        "NetProfit": 14.58, "Margin": 5.72, "Real ACOS": 21.83, "Sessions": 26.0, "Unit Session Percentage": 65.38,
        "Ads spend": -55.63, "ROI": 29.57, "Fulfillment Channel": "FBA",
    }
    row.update(overrides)
    return row


def write_workbook(path: Path, rows: list[dict], headers: list[str] | None = None) -> Path:
    headers = headers or REAL_HEADERS
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "TDSheet"
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(h) for h in headers])
    book.save(path)
    return path


EXPORT_NAME = "Econour_Dashboard_by_product_12_10_2026-17_10_2026_(2026_10_15_12_59_19_717)_sellerboard.com.xlsx"


@pytest.fixture
def log():
    return logging.getLogger("sellerboard.tests")


@pytest.fixture(autouse=True)
def project_dirs(tmp_path, monkeypatch):
    """Every test gets its own data/ and logs/ folders, never the project's."""
    data, logs = tmp_path / "data", tmp_path / "logs"
    monkeypatch.setenv("SELLERBOARD_DATA_DIR", str(data))
    monkeypatch.setenv("SELLERBOARD_LOG_DIR", str(logs))
    # Never the real Sellerboard account: a test must not be able to sign in for real.
    monkeypatch.setenv("SELLERBOARD_LOGIN_EMAIL", "")
    monkeypatch.setenv("SELLERBOARD_LOGIN_PASSWORD", "")
    return data
