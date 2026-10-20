"""Column catalogue shared by the Excel reader, the staging table and the Power BI sync.

Staging columns keep the Sellerboard header names, as powerbidb.Uk_breakdown_data
does, so one value can be traced by one name from the workbook to both tables.

Sources (inspected 2026-10-15):
* the "Dashboard by product" export (63 headers), and
* powerbidb.Uk_breakdown_data (96 data columns + auto-increment ``id``; no unique key).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    kind: str  # "int" | "decimal" | "text"
    aliases: tuple[str, ...] = ()
    max_length: int | None = None


def _money(name: str, *aliases: str) -> Column:
    return Column(name, "DECIMAL(18,4)", "decimal", aliases)


def _percent(name: str) -> Column:
    return Column(name, "DECIMAL(12,4)", "decimal")


def _count(name: str) -> Column:
    return Column(name, "INT", "int")


# The grain of one Sellerboard row. ASIN alone is not unique: one ASIN has several
# SKUs on the same day and marketplace (106 such rows in the 12-15 Oct 2026 export).
KEY_COLUMNS: tuple[Column, ...] = (
    Column("Date", "DATE", "date"),
    Column("Marketplace", "VARCHAR(64)", "text", max_length=64),
    Column("ASIN", "VARCHAR(32)", "text", max_length=32),
    Column("SKU", "VARCHAR(128)", "text", max_length=128),
)
KEY_NAMES: tuple[str, ...] = tuple(c.name for c in KEY_COLUMNS)

# Value columns that exist in powerbidb.Uk_breakdown_data, in its column order.
DESTINATION_VALUE_COLUMNS: tuple[Column, ...] = (
    _money("SalesOrganic"),
    _money("SalesPPC"),
    _money("SalesSponsoredProducts"),
    _money("SalesSponsoredDisplay"),
    _count("UnitsOrganic"),
    _count("UnitsPPC"),
    _count("UnitsSponsoredProducts"),
    _count("UnitsSponsoredDisplay"),
    _count("Refunds"),
    _money("PromoValue"),
    _money("SponsoredProducts"),
    _money("SponsoredDisplay"),
    # Sellerboard spells this header with a Cyrillic "В" (U+0412); the destination
    # column has always been SB, which is why the original script renamed it.
    _money("SB", "SponsoredВrands", "SponsoredBrands"),
    _money("SponsoredBrandsVideo"),
    _money("Google ads"),
    _money("Facebook ads"),
    _money("GiftWrap"),
    _money("Shipping"),
    _money("Refund Commission"),
    _money("Refund Goodwill"),
    _money("Refund Principal"),
    _money("Refund Promotion"),
    _money("Refund RefundCommission"),
    _money("Refund RestockingFee"),
    _money("Value of returned items"),
    _money("ProductCost Unsellable Refunds"),
    _money("Adjustment_FBAPerUnitFulfillmentFee"),
    _money("Commission"),
    _money("COMPENSATED_CLAWBACK"),
    _money("CouponRedemptionFee"),
    _money("FBADisposalFee"),
    _money("FBAInboundTransportationFee"),
    _money("FBAInternationalInboundFreightFee"),
    _money("FBALongTermStorageFee"),
    _money("FBAPerUnitFulfillmentFee"),
    _money("(MCF)"),
    _money("FBARemovalFee"),
    _money("FBAStorageFee"),
    _money("FREE_REPLACEMENT_REFUND_ITEMS"),
    _money("INCORRECT_FEES_NON_ITEMIZED"),
    _money("LiquidationsBrokerageFee"),
    _money("LiquidationsRevenue"),
    _money("MiscAdjustment"),
    _money("MISSING_FROM_INBOUND"),
    _money("REVERSAL_REIMBURSEMENT"),
    _money("RunLightningDealFee"),
    _money("Subscription"),
    _money("VineFee"),
    _money("WAREHOUSE_DAMAGE"),
    _money("WAREHOUSE_LOST"),
    _money("WAREHOUSE_LOST_MANUAL"),
    _money("EstimatedPayout"),
    _money("ProductCost Sales"),
    _money("ProductCost Non-Amazon"),
    _money("ProductCost MultichannelCosts"),
    _money("ProductCost MissingFromInbound"),
    _money("ProductCost CostOfMissingReturns"),
    _money("VAT"),
    _money("GrossProfit"),
    _money("NetProfit"),
    _percent("Margin"),
    _percent("Real ACOS"),
    _count("Sessions"),
    _percent("Unit Session Percentage"),
    _money("FBATransportationFee"),
    _money("FeeAdjustment"),
    _money("LiquidationsRevenueAdjustment"),
    _money("MISSING_FROM_INBOUND_CLAWBACK"),
    _money("MULTICHANNEL_ORDER_LOST"),
    _money("REMOVAL_ORDER_LOST"),
    _money("STARStorageBilling"),
    _money("STARStorageFee"),
    _money("WAREHOUSE_DAMAGE_EXCEPTION"),
    _money("Refund ShippingCharge"),
    _money("Refund ShippingHB"),
    _money("CommissionCorrection"),
    _money("CS_ERROR_ITEMS"),
    _money("INCORRECT_FEES_ITEMS"),
    _money("PaidServicesFee"),
    _money("FBAMultitierPerUnitFee"),
    _money("SalesCommission"),
    _money("NonSubscriptionFeeAdj"),
    _money("ReCommerceGradingAndListingCharge"),
    _money("AmazonUpstreamProcessingFee"),
    _money("AmazonUpstreamStorageTransportationFee"),
    _money("LabelingFee"),
    _money("Refund ReturnShipping"),
    _money("CapacityReservationFee"),
    _money("FBAInboundConvenienceFee"),
    _money("FBAInboundDefectFee"),
    _money("PoAPerUnitFulfillmentFee"),
    _money("POAServiceFee"),
)

# Workbook columns the destination has no column for. They are kept in staging
# (and reported by the sync) rather than dropped. No rename onto a similar-looking
# destination column is assumed: e.g. "Refund GoodwillPrincipal" is not mapped to
# "Refund Goodwill" (empty in every destination row since 2026-09).
STAGING_ONLY_COLUMNS: tuple[Column, ...] = (
    Column("Name", "TEXT", "text", max_length=65535),
    _money("Refund DigitalServicesFee"),
    _money("Refund GoodwillPrincipal"),
    _money("Refund ShippingChargeback"),
    _money("Refund ShippingTax"),
    _money("Refund ShippingTaxDiscount"),
    _money("Refund ShipPromotion"),
    _money("Refund TaxDiscount"),
    _money("RefundsCostsDamaged"),
    _money("DigitalServicesFee"),
    _money("Ads spend"),
    _percent("Sellable Returns %"),
    _percent("ROI"),
    _money("Expenses"),
    # Totals Sellerboard writes instead of the itemised columns when an export is made
    # without "Show detailed breakdown of Amazon fees, refund costs and COGS".
    _money("AmazonFees"),
    _money("RefundCost"),
    _money("Cost of Goods"),
    Column("Fulfillment Channel", "VARCHAR(32)", "text", max_length=32),
)

VALUE_COLUMNS: tuple[Column, ...] = DESTINATION_VALUE_COLUMNS + STAGING_ONLY_COLUMNS
ALL_COLUMNS: tuple[Column, ...] = KEY_COLUMNS + VALUE_COLUMNS
COLUMNS_BY_NAME: dict[str, Column] = {c.name: c for c in ALL_COLUMNS}

# Explicit staging -> destination mapping. Every destination column has the same
# name as its staging column; staging-only columns are deliberately absent.
DESTINATION_COLUMN_MAP: dict[str, str] = {
    c.name: c.name for c in KEY_COLUMNS + DESTINATION_VALUE_COLUMNS
}

# A workbook without these is not a Sellerboard product breakdown and is refused.
REQUIRED_COLUMNS: tuple[str, ...] = KEY_NAMES + (
    "SalesOrganic", "SalesPPC", "UnitsOrganic", "UnitsPPC",
    "GrossProfit", "NetProfit", "EstimatedPayout",
)


def header_lookup() -> dict[str, str]:
    """Workbook header (stripped) -> staging column name."""
    lookup: dict[str, str] = {}
    for column in ALL_COLUMNS:
        for header in (column.name, *column.aliases):
            lookup[header] = column.name
    return lookup


def quote(name: str, *, params: bool = False) -> str:
    """Backtick-quote a MySQL identifier taken from this catalogue or a live schema.

    Pass ``params=True`` for a statement executed with parameters: PyMySQL
    %-formats those, so a "%" in a name ("Sellable Returns %") must be doubled.
    """
    if "`" in name or not name:
        raise ValueError(f"unsafe identifier {name!r}")
    quoted = f"`{name}`"
    return quoted.replace("%", "%%") if params else quoted
