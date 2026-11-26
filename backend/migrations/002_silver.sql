-- Bronze -> Silver bookkeeping in the control schema. The Silver tables themselves
-- (column_mapping, detail, summary) are created by the service in AHI_SILVER_SCHEMA.

-- The AHI doc's eligibility rule reads straight off the ingestion audit table:
-- status = 'ingested' AND silver_status IS DISTINCT FROM 'succeeded'.
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS silver_status text;
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS silver_run_id text;
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS silver_loaded_at timestamptz;

-- One row per approved Silver run: who approved it, and the full mapping they approved
-- with how each column was matched (method, confidence).
CREATE TABLE IF NOT EXISTS {control}.silver_run (
    id            text PRIMARY KEY,
    ingestion_ids text[] NOT NULL,
    reviewed_by   text NOT NULL,
    mapping       jsonb NOT NULL,
    quality       jsonb,
    rows_loaded   integer NOT NULL DEFAULT 0,
    rows_removed  integer NOT NULL DEFAULT 0,
    status        text NOT NULL,
    error         text,
    approved_at   timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

-- Placeholder for the AHI doc's LOTL until the real table is available
-- (AHI_LOTL_TABLE points elsewhere when it is).
CREATE TABLE IF NOT EXISTS {control}.lotl (
    pc_id                text PRIMARY KEY,
    legacy_office_name   text NOT NULL,
    profit_center_number text NOT NULL
);
