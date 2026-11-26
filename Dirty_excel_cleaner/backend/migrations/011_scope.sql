-- The scope (AJG / APD / BOTH) a file's profit center is validated against: which required
-- columns apply (required_column_by_scope). Already on the live control table; formalized
-- here for a fresh database.
ALTER TABLE {control}.control_table ADD COLUMN IF NOT EXISTS scope text;

-- BOTH's required columns: the union of APD's and AJG's, which (APD's being a subset of
-- AJG's) is exactly AJG's list.
INSERT INTO {bronze}.required_column_by_scope (silver_column_name, scope) VALUES
    ('premium', 'BOTH'),
    ('producer_agency_name', 'BOTH'),
    ('producer_commission_amount', 'BOTH'),
    ('producer_commission_pct', 'BOTH'),
    ('gross_commission_amount', 'BOTH'),
    ('commission_pct', 'BOTH'),
    ('policy_number', 'BOTH'),
    ('transaction_effective_date', 'BOTH'),
    ('accounting_effective_date', 'BOTH'),
    ('policy_effective_date', 'BOTH'),
    ('insurance_company_name', 'BOTH')
ON CONFLICT DO NOTHING;
