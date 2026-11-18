-- Correction: AJG (and BOTH, its union with APD) needs policy_effective_date, not
-- policy_expiration_date. 010 and 011 are fixed for a fresh database; this corrects one
-- already migrated.
UPDATE {bronze}.required_column_by_scope SET silver_column_name = 'policy_effective_date'
WHERE scope IN ('AJG', 'BOTH') AND silver_column_name = 'policy_expiration_date';
