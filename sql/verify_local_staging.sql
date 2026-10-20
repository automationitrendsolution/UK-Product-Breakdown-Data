-- Read-only checks of the local staging database (sellerboard). Nothing here writes.
--
-- PowerShell:
--   Get-Content sql\verify_local_staging.sql | docker exec -i sellerboard-mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u"$MYSQL_USER" sellerboard --table'
--
-- Checks the newest imported batch. For another batch, replace the SET line with:
--   SET @batch_id = '<batch id>';

SET @batch_id = (SELECT batch_id FROM import_batches WHERE status = 'imported' ORDER BY created_at DESC LIMIT 1);

-- 0. Import batches, newest first (failed attempts are listed too; they hold no staging rows)
SELECT batch_id, created_at, status, validation_status, sync_status, start_date, end_date,
       file_count, rows_read, rows_valid, rows_duplicate, rows_rejected,
       rows_excluded_date, rows_excluded_marketplace
FROM import_batches ORDER BY created_at DESC LIMIT 10;

-- 1. Staging records of the batch (first 25)
SELECT staging_id, validation_status, `Date`, `Marketplace`, `ASIN`, `SKU`,
       `SalesOrganic`, `SalesPPC`, `UnitsOrganic`, `UnitsPPC`, `SponsoredProducts`, `SB`,
       `Refunds`, `PromoValue`, `GrossProfit`, `NetProfit`, `Margin`, `ROI`, `Sessions`, sync_status
FROM uk_breakdown_staging
WHERE batch_id = @batch_id
ORDER BY `Date`, `Marketplace`, `ASIN`, `SKU`
LIMIT 25;

-- 2. Minimum and maximum dates
SELECT MIN(`Date`) AS min_date, MAX(`Date`) AS max_date, COUNT(*) AS valid_rows
FROM uk_breakdown_staging
WHERE batch_id = @batch_id AND validation_status = 'valid';

-- 3. Rows per reporting date
SELECT `Date`, COUNT(*) AS valid_rows,
       SUM(COALESCE(`SalesOrganic`, 0) + COALESCE(`SalesPPC`, 0)) AS sales,
       SUM(COALESCE(`UnitsOrganic`, 0) + COALESCE(`UnitsPPC`, 0)) AS units,
       SUM(COALESCE(`NetProfit`, 0)) AS net_profit
FROM uk_breakdown_staging
WHERE batch_id = @batch_id AND validation_status = 'valid'
GROUP BY `Date` ORDER BY `Date`;

-- 4. Distinct ASINs and SKUs
SELECT COUNT(DISTINCT `ASIN`) AS distinct_asins, COUNT(DISTINCT `SKU`) AS distinct_skus,
       COUNT(DISTINCT `ASIN`, `SKU`) AS distinct_asin_sku
FROM uk_breakdown_staging
WHERE batch_id = @batch_id AND validation_status = 'valid';

-- 5. Marketplace values
SELECT `Marketplace`, COUNT(*) AS valid_rows
FROM uk_breakdown_staging
WHERE batch_id = @batch_id AND validation_status = 'valid'
GROUP BY `Marketplace` ORDER BY valid_rows DESC;

-- 6a. Duplicate Date/Marketplace/ASIN/SKU among valid rows (expect no rows)
SELECT `Date`, `Marketplace`, `ASIN`, `SKU`, COUNT(*) AS copies
FROM uk_breakdown_staging
WHERE batch_id = @batch_id AND validation_status = 'valid'
GROUP BY `Date`, `Marketplace`, `ASIN`, `SKU`
HAVING COUNT(*) > 1;

-- 6b. Rows the import flagged as duplicate or rejected, with the reason
SELECT staging_id, source_file, source_row_number, validation_status, validation_errors
FROM uk_breakdown_staging
WHERE batch_id = @batch_id AND validation_status <> 'valid'
ORDER BY source_file, source_row_number;

-- 7. Sync outcome per row, and the sync attempts for the batch
SELECT sync_status, COUNT(*) AS staged_rows
FROM uk_breakdown_staging
WHERE batch_id = @batch_id AND validation_status = 'valid'
GROUP BY sync_status;

SELECT run_id, started_at, finished_at, status, update_existing, rows_considered, rows_inserted,
       rows_updated, rows_unchanged, rows_conflict, rows_rejected,
       destination_rows_before, destination_rows_after, LEFT(error_message, 200) AS error
FROM sync_runs WHERE batch_id = @batch_id ORDER BY run_id;

-- 8. Source counts per Date/Marketplace: compare with query 3 of verify_powerbi_destination.sql
SELECT `Date`, `Marketplace`, COUNT(*) AS staged_rows,
       SUM(sync_status IN ('inserted', 'updated', 'unchanged')) AS in_powerbi_after_sync
FROM uk_breakdown_staging
WHERE batch_id = @batch_id AND validation_status = 'valid'
GROUP BY `Date`, `Marketplace`
ORDER BY `Date`, `Marketplace`;
