# Per-profit-center Silver rules: seed files

One YAML file per profit center. `backend/tools/load_pc_rules.py` validates them and loads
them into `<control>.silver_pc_rule`; reviewers then edit the rules in the Silver page's
Rules tab. The loader never overwrites a rule someone edited in the UI unless `--force` is given.

They were written from the July 2026 Databricks `*_cleansed.py` notebooks
(`E:\LR 1\silver\silver`). Every rule names its notebook and line in `source_ref`.

## A rule

```yaml
- key: pc2041.retention.issued_only     # unique within the profit center
  kind: filter                          # filter | date_format | lookup | translate | derive | constant
  target: transaction                   # transaction (default) | aggregate | both
  scope_file_like: "*Preferra Production*"   # optional globs, case-insensitive
  scope_sheet: Renewal
  scope_bronze_table: null
  column: null                          # the Silver column written (column kinds only)
  seq: 10                               # order within a kind
  enabled: true
  spec: {keep: {op: eq, args: ["src:Renewal Sales Cycle Status", ISSUED]}}
  note: ...
  source_ref: ext_pc2041_retention_silver_cleansed.py:338
```

Operands: `"src:<bronze column>"` (the header as written; matched without case or
spacing), `"silver:<column>"` (a value computed before this rule), a literal, or an
expression `{op: ..., args: [...]}`. See `backend/src/ahi_silver/rules.py` for the ops.

Order for a load: filter, date_format, lookup, translate, derive, constant (then `seq`).
Rules run before the profit center is settled from the LOTL.

## Not carried over from the notebooks

* DRT reporting dates: the tool derives them from the control table.
* Profit-center name/number literals: the LOTL fills them from the load's pc_id; a name
  the LOTL does not know would make the row `no_match`.
* Month names, the year/month-from-date fallback and `MM-dd-yyyy` / `M/d/yyyy` dates:
  the Silver cleansing does these for every file.
* Plain column renames: that is the DRT mapping's job.
