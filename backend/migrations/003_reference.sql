-- The business's reference tables (assets/ folder) and the extra bronze columns.
-- The rows themselves are loaded by api/services/reference_service.py when a table is
-- empty, and can be reloaded with backend/tools/seed_reference.py.

-- Division lookup for bronze's division_name, keyed by the 4-digit profit center.
-- A profit center can appear under two divisions; the Ingest reviewer then picks one.
CREATE TABLE IF NOT EXISTS {control}.division_mapping (
    division             text,
    international_office text,
    profit_center        text
);
CREATE INDEX IF NOT EXISTS division_mapping_profit_center ON {control}.division_mapping (profit_center);

-- The LOTL is the business's pc_name_pc_number_from_lotl table. The earlier placeholder
-- (pc_id, legacy_office_name, profit_center_number) held test rows only; it is replaced.
DROP TABLE IF EXISTS {control}.lotl;
CREATE TABLE {control}.lotl (
    profit_center_number text,
    legacy_office_name   text,
    status               text
);
CREATE INDEX IF NOT EXISTS lotl_profit_center_number ON {control}.lotl (profit_center_number);

-- Each bronze load's file-level values, also written on every row of its bronze table.
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS pc_id text;
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS file_date date;
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS division_name text;
-- The bronze load time (bronze processing_date); Silver's ingestion_timestamp, and with
-- table and file name the identity of a load's rows in silver_detail.
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS processing_date timestamptz;
