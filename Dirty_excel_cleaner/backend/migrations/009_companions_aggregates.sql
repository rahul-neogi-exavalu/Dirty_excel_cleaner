-- Companion files, the profit center's own aggregates, saved Silver joins, and Silver's
-- transaction table.

-- A file that came with another (a broker list beside the transactions): the control row
-- of the file it came with. The two are checked together and joined in Silver.
ALTER TABLE {control}.control_table ADD COLUMN IF NOT EXISTS companion_of integer;

-- Y: the profit center sent figures it had already aggregated (a summary), not its
-- transactions. They load into an ``_agg`` bronze table (staged as ``_agg_stg``) and from
-- there into Silver's aggregate table, never its transaction table.
ALTER TABLE {control}.control_table ADD COLUMN IF NOT EXISTS is_aggregated char(1) NOT NULL DEFAULT 'N';
DO $$
BEGIN
    ALTER TABLE {control}.control_table
        ADD CONSTRAINT control_table_is_aggregated_check CHECK (is_aggregated IN ('Y', 'N'));
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;
ALTER TABLE {control}.ingestion ADD COLUMN IF NOT EXISTS is_aggregated char(1) NOT NULL DEFAULT 'N';

-- A join of two bronze tables the reviewer approved in Silver (a profit center's
-- transactions with the broker list that came with them), offered again the next time
-- the same two tables come in. ``keys``: [{"left": header, "right": header}], as the files
-- write them.
CREATE TABLE IF NOT EXISTS {control}.silver_join (
    profit_center text        NOT NULL,
    left_table    text        NOT NULL,
    right_table   text        NOT NULL,
    how           text        NOT NULL CHECK (how IN ('left', 'right', 'inner')),
    keys          jsonb       NOT NULL,
    ignore_case   boolean     NOT NULL DEFAULT true,
    approved_by   text,
    approved_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profit_center, left_table, right_table)
);

-- silver_detail is Silver's transaction table, and is named so. The aggregate rows rolled
-- up from it say where they came from by its name.
DO $$
BEGIN
    IF to_regclass('{silver}.silver_detail') IS NOT NULL AND to_regclass('{silver}.silver_transaction') IS NULL THEN
        ALTER TABLE {silver}.silver_detail RENAME TO silver_transaction;
        ALTER INDEX IF EXISTS {silver}.silver_detail_load RENAME TO silver_transaction_load;
    END IF;
    IF to_regclass('{silver}.silver_aggregate') IS NOT NULL THEN
        UPDATE {silver}.silver_aggregate SET agg_data_source = 'silver_transaction'
            WHERE agg_data_source = 'silver_detail';
        UPDATE {silver}.silver_aggregate
            SET source_table = regexp_replace(source_table, '[.]silver_detail$', '.silver_transaction')
            WHERE source_table LIKE '%.silver_detail';
    END IF;
END $$;

-- How the reviewer mapped a profit center's aggregated table onto silver_aggregate,
-- offered again the next time that table comes in: each bronze column's aggregate
-- column, and the columns spread across a dimension (their measure, and the column their
-- headers fill).
CREATE TABLE IF NOT EXISTS {control}.silver_aggregate_mapping (
    profit_center    text        NOT NULL,
    bronze_table     text        NOT NULL,
    bronze_column    text        NOT NULL,
    silver_column    text,
    spread_measure   text,
    spread_dimension text,
    approved_by      text,
    approved_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profit_center, bronze_table, bronze_column)
);
