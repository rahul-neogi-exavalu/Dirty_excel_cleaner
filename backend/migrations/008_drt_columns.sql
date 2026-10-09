-- The business's DRT columns, each with the Silver column it means.
--
-- drt_label used to hold only the DRT labels the DRT mapping workbook happens to use (21 of
-- them), so a mapping to any other DRT column ("Producer Office City") was saved without
-- its DRT column. It now holds every DRT column of the Silver schema
-- (backend/config/silver_columns.csv) with its lowercase_with_underscores Silver column,
-- kept in step at startup by reference_service.sync_drt_columns, which also gives the
-- drt_column_mapping rows saved without one their DRT column.
ALTER TABLE {control}.drt_label ADD COLUMN IF NOT EXISTS silver_column_name text;

-- One DRT column per Silver column.
CREATE UNIQUE INDEX IF NOT EXISTS drt_label_silver_column ON {control}.drt_label (silver_column_name)
    WHERE silver_column_name IS NOT NULL;
