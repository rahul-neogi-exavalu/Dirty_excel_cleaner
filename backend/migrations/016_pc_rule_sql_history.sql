-- Per-profit-center rules: their SQL view, when they were made and changed, and a soft
-- delete that keeps a deleted rule's row as history.
--
-- sql_text  the rule's logic as SQL (backend/src/ahi_silver/rule_sql.py): what the
--           reviewer wrote, when they saved it as SQL, else the SQL its JSON reads as.
--           NULL when the rule has no exact SQL form (lookups, date formats, and the
--           few specs SQL cannot show). The JSON spec is always what runs.
-- has_sql   whether the rule has that SQL form.
-- is_deleted / deleted_at / deleted_by: a deleted rule is kept, never applied, never listed
--           by default; its key may be used again.
ALTER TABLE {control}.silver_pc_rule
    ADD COLUMN IF NOT EXISTS sql_text    text,
    ADD COLUMN IF NOT EXISTS has_sql     boolean     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS created_at  timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS modified_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS is_deleted  boolean     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS deleted_at  timestamptz,
    ADD COLUMN IF NOT EXISTS deleted_by  text;

-- Rules made before these columns: their last approval is the best date there is.
UPDATE {control}.silver_pc_rule SET created_at = approved_at, modified_at = approved_at
WHERE created_at > approved_at;

-- One live rule per key and profit center; deleted ones may share it.
ALTER TABLE {control}.silver_pc_rule DROP CONSTRAINT IF EXISTS silver_pc_rule_profit_center_rule_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS silver_pc_rule_live_key
    ON {control}.silver_pc_rule (profit_center, rule_key) WHERE NOT is_deleted;

DROP INDEX IF EXISTS {control}.silver_pc_rule_pc;
CREATE INDEX IF NOT EXISTS silver_pc_rule_pc ON {control}.silver_pc_rule (profit_center) WHERE enabled AND NOT is_deleted;
