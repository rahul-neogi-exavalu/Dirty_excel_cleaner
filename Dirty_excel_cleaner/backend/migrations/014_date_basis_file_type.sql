-- The reviewer's manual date basis, reporting file type and data collection period: the
-- control table's computed reporting dates (drt_reporting_start_date/end_date) and the
-- business's YTD/MONTHLY classification now come from these instead of auto-detection.
ALTER TABLE {control}.control_table ADD COLUMN IF NOT EXISTS date_basis text
    CHECK (date_basis IS NULL OR date_basis IN ('AED', 'PED', 'TED'));
ALTER TABLE {control}.control_table ADD COLUMN IF NOT EXISTS reporting_file_type text
    CHECK (reporting_file_type IS NULL OR reporting_file_type IN ('INCREMENTAL', 'CUMULATIVE'));
ALTER TABLE {control}.control_table ADD COLUMN IF NOT EXISTS data_collection_period date;
