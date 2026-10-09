-- The Silver Cleansed stage, Silver reviews kept in the database, and the DRT column
-- mapping's table moved out of request handling. {silver} is AHI_SILVER_SCHEMA and
-- {cleansed} AHI_CLEANSED_SCHEMA.

CREATE SCHEMA IF NOT EXISTS {silver};
-- The AHI doc's source-specific Silver Cleansed tables: one per bronze table, same name.
CREATE SCHEMA IF NOT EXISTS {cleansed};

-- The business's DRT column mapping (assets/drt_column_mapping.xlsx) plus the Silver
-- column each row means. Filled from the workbook by reference_service while empty.
CREATE TABLE IF NOT EXISTS {silver}.drt_column_mapping (
    profit_center      text NOT NULL,
    pc_column          text NOT NULL,
    drt_column         text,
    silver_column_name text
);
-- Approved ignores used to be saved as rows with neither a DRT nor a Silver column.
-- Ignores are no longer saved (a column is simply asked about again), and the workbook
-- has no such rows, so every one of them was written by the app.
DELETE FROM {silver}.drt_column_mapping WHERE drt_column IS NULL AND silver_column_name IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS drt_column_mapping_unique ON {silver}.drt_column_mapping
    (profit_center, pc_column, coalesce(silver_column_name, ''), coalesce(drt_column, ''));

-- The DRT labels the business uses (the workbook's distinct DRT_Column values). Only these
-- are written to drt_column_mapping.drt_column; any other mapping keeps it NULL.
CREATE TABLE IF NOT EXISTS {control}.drt_label (
    label text PRIMARY KEY
);

-- Each load's original source headers ({bronze column: header as the file wrote it}) and
-- the table regions it came from (two tables on one sheet are two loads, not a duplicate).
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS source_headers jsonb;
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS source_regions text[] NOT NULL DEFAULT '{}';

-- Which cleansed table holds a bronze table's cleansed rows.
CREATE TABLE IF NOT EXISTS {control}.silver_cleansed_table (
    bronze_table   text PRIMARY KEY,
    cleansed_table text NOT NULL UNIQUE,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- A Silver review, from creation to the end of its load: every API process sees the same
-- one, and it survives a restart. ``state`` is the whole review (tables, loads, every
-- method's votes, the reviewer's choices, the quality report); bronze rows are not stored.
CREATE TABLE IF NOT EXISTS {control}.silver_draft (
    id            text PRIMARY KEY,
    ingestion_ids text[] NOT NULL DEFAULT '{}',
    status        text NOT NULL,
    state         jsonb NOT NULL,
    version       integer NOT NULL DEFAULT 1,
    progress      real NOT NULL DEFAULT 0,
    message       text NOT NULL DEFAULT '',
    error         jsonb,
    result        jsonb,
    reviewed_by   text,
    approved_by   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    expires_at    timestamptz
);
CREATE INDEX IF NOT EXISTS silver_draft_status ON {control}.silver_draft (status, expires_at);

-- Who approved a Silver run (the signed-in user), besides the reviewer's name.
ALTER TABLE {control}.silver_run ADD COLUMN IF NOT EXISTS approved_by text;
