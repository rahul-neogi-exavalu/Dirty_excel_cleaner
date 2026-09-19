# AHI POC — Schema-free Excel cleaning pipeline

Turns report-shaped Excel exports (logos, banners, footers, blank gutters, subtotals,
transposed layouts, multi-sheet workbooks) into flat, table-ready CSVs — with an audit
report explaining every structural decision.

**No schema, no aliases, no keyword lists.** The pipeline has no idea what a "premium" is.
Every decision comes from the data's own shape, types, distinctness, density and
arithmetic, so a file with column names it has never seen is handled by the same code path
as a known one. Output columns are the file's own labels, normalized.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical design and diagrams.

## Run

```bash
pip install -r requirements.txt
```

Run from the project root. `clean.py` puts `src/` on the path itself, so nothing needs
setting first — this works the same in PowerShell, cmd and bash.

```bash
python clean.py
```

With no arguments it cleans the bundled samples into `cleaned/` and `audit/`. To point it
at other files:

```bash
python clean.py "some_folder/*.xlsx" --out cleaned --audit audit
```

```bash
python -m pytest
```

## Outputs

| CSV | Source | Rows |
|---|---|---|
| `File1_Scenarios1-4_Combined.csv` | leading blank row/column, gutter column, blank row mid-data | 10 |
| `File2_Scenarios5-8_Combined.csv` | logo, title banner, timestamp, confidentiality line, grand total, footer | 10 |
| `File3_Scenario_subtotals_in_middle.csv` | three subtotal rows spliced between groups | 10 |
| `File4_Scenario_transposed.csv` | fields down column 1, records across | 10 |
| `File5_Scenario_MultiSheet.csv` | Jan + Feb stacked, with a `source_sheet` column | 20 |
| `File6_Scenarios_MultipleSheetJoin.csv` | Report joined to the Producer lookup | 10 |
| `File6_Scenarios_MultipleSheetJoin_Producer.csv` | the lookup, on its own | 5 |

## How it decides, in one line each

**Regions.** A sheet may hold several tables. Blank rows are grouped by whether the column
*type profile* matches across the gap; blank columns by whether the two sides span the same
*rows*. **Gap size is never an input** — one blank row and twelve behave identically.

**Orientation.** Each column of an upright table holds one type and its header labels are
distinct; a transposed table reverses both. File4's column 1 is fully distinct (1.00) while
its row 1 repeats `South Zone PC` four times (0.45).

**Header.** Scored on type contrast with the columns below, textness, uniqueness, fill and
brevity — plus bold/fill/border as a bonus. Below 0.55 the pipeline **refuses to pick one**
and names columns positionally, because naming a data row as the header is the worse
failure.

**Junk rows.** Sparsity finds candidates; **arithmetic confirms them** — a total is a row
whose number equals the sum of the rows above it. A profit centre named `Total Risk PC`
survives because it is not sparse and its numbers do not sum.

**Join keys.** Found from values, never names — without a schema the two columns are
literally called `producer_agencyname` and `producer`, and no name matching would pair them.
Fuzzy matches auto-resolve only if they clear 90 **and** beat the runner-up by 10.

**Types.** Inferred per column. Leading zeros, and near-unique integers of constant width,
are kept as text — which recovers structurally that `1005` and `PC0001` are both identifiers.

## Verification

**Reconciliation.** The junk the pipeline strips is the oracle for the data it keeps.
File2's discarded grand total (`230,712.19`) and File3's three subtotals equal the sums of
the cleaned rows exactly — and they now pass via arithmetic detection, so the check tests
the real mechanism rather than restating a regex.

**Ablation.** Disabling every text pattern in the codebase changes no output. Disabling
sparsity, arithmetic or orientation breaks specific files. That is the measured proof the
hardcoding is gone — full table in [ARCHITECTURE.md §12](ARCHITECTURE.md#12-ablation-what-is-actually-load-bearing).

**Unseen schema.** A file with entirely different column names, a 3-row mid-table gap, a
gutter column, nested totals and a German footer cleans correctly and reconciles.

118 tests.

## Layout

```
clean.py            entry point (no PYTHONPATH needed)
src/ahi_clean/
  signals.py        scoring primitives (fill, uniqueness, type profile, contrast)
  typing_utils.py   fine-grained type inference
  reader.py         workbook -> cell grid + formatting; nulls error cells
  geometry.py       sheet -> table regions; all blank-gap handling
  header.py         header scoring, the no-header path, column naming
  rowclass.py       sparsity + arithmetic row classification
  extract.py        per region: orient -> header -> classify -> type -> validate
  orchestrate.py    table roles, stack/join decisions
  join.py           value-based key discovery with the margin rule
  audit.py          trace -> JSON report
```

## Known limits

- **Semantic mapping is out of scope by construction.** Deciding that `Producer` in one file
  and `Agent Name` in another belong in the same target column needs a schema, a config, an
  LLM or a human. The pipeline stops at per-file-correct structure and records every
  inferred type.
- `08085` stored in Excel as the number `8085` is unrecoverable — the zero was gone before
  the pipeline saw the file.
- Orientation needs ≥2 data rows; near-square tables fall back to a shape tiebreak and are
  flagged `confident: false`.
