-- Per-profit-center Silver rules: what the manual notebooks did for one profit center's
-- files -- a row filter, a date written day first, an address looked up, a code
-- translated, a column derived, a value the file never states -- kept as data
-- (backend/src/ahi_silver/rules.py). Seeded from backend/config/pc_rules/*.yaml by
-- backend/tools/load_pc_rules.py; edited in the Silver page's Rules tab.
CREATE TABLE IF NOT EXISTS {control}.silver_pc_rule (
    id                 bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profit_center      text        NOT NULL,  -- PC2041
    rule_key           text        NOT NULL,  -- pc2041.retention.issued_only
    kind               text        NOT NULL
        CHECK (kind IN ('filter', 'date_format', 'lookup', 'translate', 'derive', 'constant')),
    target             text        NOT NULL DEFAULT 'transaction'
        CHECK (target IN ('transaction', 'aggregate', 'both')),
    column_name        text,                  -- the Silver column written (column kinds)
    scope_bronze_table text,                  -- globs, case-insensitive; NULL: any
    scope_file_like    text,
    scope_sheet        text,
    seq                integer     NOT NULL DEFAULT 100,
    spec               jsonb       NOT NULL,
    enabled            boolean     NOT NULL DEFAULT true,
    note               text,
    source_ref         text,                  -- the notebook and line it came from
    approved_by        text,                  -- 'seed' until a person edits it
    approved_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (profit_center, rule_key)
);
CREATE INDEX IF NOT EXISTS silver_pc_rule_pc ON {control}.silver_pc_rule (profit_center) WHERE enabled;

-- Bumped by every change to the rules, so a review approved against older rules is run
-- again rather than loaded with values the reviewer never saw.
CREATE TABLE IF NOT EXISTS {control}.silver_pc_rule_version (
    id      boolean     PRIMARY KEY DEFAULT true CHECK (id),
    version bigint      NOT NULL DEFAULT 0,
    changed timestamptz NOT NULL DEFAULT now()
);
INSERT INTO {control}.silver_pc_rule_version (id, version) VALUES (true, 0) ON CONFLICT (id) DO NOTHING;
