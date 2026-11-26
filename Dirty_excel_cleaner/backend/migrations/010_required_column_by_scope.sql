-- Which required columns (Silver names) apply to each scope (source system), seeded from
-- the business's per-scope lists.
CREATE TABLE IF NOT EXISTS {bronze}.required_column_by_scope (
    silver_column_name text NOT NULL,
    scope               text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope, silver_column_name)
);

INSERT INTO {bronze}.required_column_by_scope (silver_column_name, scope) VALUES
    ('producer_agency_name', 'APD'),
    ('insurance_company_name', 'APD'),
    ('premium', 'APD'),
    ('premium', 'AJG'),
    ('producer_agency_name', 'AJG'),
    ('producer_commission_amount', 'AJG'),
    ('producer_commission_pct', 'AJG'),
    ('gross_commission_amount', 'AJG'),
    ('commission_pct', 'AJG'),
    ('policy_number', 'AJG'),
    ('transaction_effective_date', 'AJG'),
    ('accounting_effective_date', 'AJG'),
    ('policy_effective_date', 'AJG'),
    ('insurance_company_name', 'AJG')
ON CONFLICT DO NOTHING;
