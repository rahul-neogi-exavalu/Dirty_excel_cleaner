-- The DRT column mapping no longer varies by profit center: the business's manually
-- curated mapping (file_column_name -> silver_column_name) applies the same way everywhere,
-- so keeping profit_center meant seeding the same mapping once per PC (redundant rows).
DROP INDEX IF EXISTS {silver}.drt_column_mapping_unique;

-- Collapse rows that only differed by profit_center before the column is dropped.
DELETE FROM {silver}.drt_column_mapping a USING {silver}.drt_column_mapping b
WHERE a.ctid > b.ctid AND a.pc_column = b.pc_column
    AND a.silver_column_name IS NOT DISTINCT FROM b.silver_column_name
    AND a.drt_column IS NOT DISTINCT FROM b.drt_column;

ALTER TABLE {silver}.drt_column_mapping DROP COLUMN IF EXISTS profit_center;

CREATE UNIQUE INDEX IF NOT EXISTS drt_column_mapping_unique ON {silver}.drt_column_mapping
    (pc_column, coalesce(silver_column_name, ''), coalesce(drt_column, ''));
