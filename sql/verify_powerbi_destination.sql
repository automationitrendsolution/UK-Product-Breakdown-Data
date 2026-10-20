-- Read-only checks of powerbidb.Uk_breakdown_data. Nothing here writes.
-- Run with MySQL Workbench (or any client) connected to the Power BI server, schema powerbidb.
-- The table name is case-sensitive on that server: Uk_breakdown_data.
--
-- The same comparison, side by side with the staged batch, is printed by:
--   python sync_uk_breakdown_to_powerbi.py --batch-id <id> --dry-run
-- (after a successful sync every staged row shows as "unchanged").

SET @start_date = '2026-10-12';
SET @end_date   = '2026-10-15';

-- 1. Destination records in the period (first 50)
SELECT id, `Date`, `Marketplace`, `ASIN`, `SKU`, `SalesOrganic`, `SalesPPC`, `UnitsOrganic`, `UnitsPPC`,
       `SponsoredProducts`, `SB`, `GrossProfit`, `NetProfit`, `Margin`, `Sessions`
FROM Uk_breakdown_data
WHERE `Date` >= @start_date AND `Date` < @end_date + INTERVAL 1 DAY
ORDER BY `Date`, `Marketplace`, `ASIN`, `SKU`
LIMIT 50;

-- 2. Date range and row count of the whole table
SELECT MIN(`Date`) AS min_date, MAX(`Date`) AS max_date, COUNT(*) AS total_rows FROM Uk_breakdown_data;

-- 3. Rows per Date/Marketplace in the period: compare with query 8 of verify_local_staging.sql
SELECT DATE(`Date`) AS `Date`, `Marketplace`, COUNT(*) AS powerbi_rows,
       COUNT(DISTINCT `ASIN`) AS distinct_asins
FROM Uk_breakdown_data
WHERE `Date` >= @start_date AND `Date` < @end_date + INTERVAL 1 DAY
GROUP BY DATE(`Date`), `Marketplace`
ORDER BY 1, 2;

-- 4. Duplicate Date/Marketplace/ASIN/SKU in the period (expect no rows)
SELECT DATE(`Date`) AS `Date`, `Marketplace`, `ASIN`, `SKU`, COUNT(*) AS copies, GROUP_CONCAT(id) AS ids
FROM Uk_breakdown_data
WHERE `Date` >= @start_date AND `Date` < @end_date + INTERVAL 1 DAY
GROUP BY DATE(`Date`), `Marketplace`, `ASIN`, `SKU`
HAVING COUNT(*) > 1;

-- 5. Marketplace values in the period
SELECT `Marketplace`, COUNT(*) AS powerbi_rows
FROM Uk_breakdown_data
WHERE `Date` >= @start_date AND `Date` < @end_date + INTERVAL 1 DAY
GROUP BY `Marketplace` ORDER BY powerbi_rows DESC;

-- 6. Totals per day (compare with query 3 of verify_local_staging.sql)
SELECT DATE(`Date`) AS `Date`, COUNT(*) AS powerbi_rows,
       SUM(COALESCE(`SalesOrganic`, 0) + COALESCE(`SalesPPC`, 0)) AS sales,
       SUM(COALESCE(`UnitsOrganic`, 0) + COALESCE(`UnitsPPC`, 0)) AS units,
       SUM(COALESCE(`NetProfit`, 0)) AS net_profit
FROM Uk_breakdown_data
WHERE `Date` >= @start_date AND `Date` < @end_date + INTERVAL 1 DAY
GROUP BY DATE(`Date`) ORDER BY 1;
