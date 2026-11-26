-- Registry and audit tables for the bronze layer. {control} is the control schema
-- (AHI_CONTROL_SCHEMA); the bronze tables themselves are created at ingestion time.

CREATE SCHEMA IF NOT EXISTS {control};

-- One row per bronze table: its columns in order, with the cleaner's datatype.
CREATE TABLE IF NOT EXISTS {control}.bronze_table (
    table_name    text PRIMARY KEY,
    schema_name   text NOT NULL,
    source_system text NOT NULL,
    columns       jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Every approved plan: what the reviewer saw and confirmed.
CREATE TABLE IF NOT EXISTS {control}.ingest_plan (
    id          text PRIMARY KEY,
    batch_id    text,
    items       jsonb NOT NULL,
    reviewed_by text NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL,
    finished_at timestamptz,
    error       text
);

-- The ingestion audit table: one row per file loaded (or skipped) into a bronze table.
-- status: ingested | superseded | skipped
CREATE TABLE IF NOT EXISTS {control}.ingestion (
    id            text PRIMARY KEY,
    plan_id       text REFERENCES {control}.ingest_plan (id),
    table_name    text NOT NULL,
    file_name     text NOT NULL,
    file_sha256   text NOT NULL,
    source_system text,
    source_sheets text[] NOT NULL DEFAULT '{}',
    period_start  text,
    period_end    text,
    action        text NOT NULL,
    schema_diff   jsonb,
    rows_loaded   integer NOT NULL DEFAULT 0,
    status        text NOT NULL,
    superseded_by text,
    reviewed_by   text,
    job_id        text,
    output_id     text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ingestion_table_status ON {control}.ingestion (table_name, status);
CREATE INDEX IF NOT EXISTS ingestion_file_sha256 ON {control}.ingestion (file_sha256);
