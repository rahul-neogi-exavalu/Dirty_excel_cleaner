# AHI POC — Excel cleaning pipeline

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical design and diagrams.

Turns report-shaped Excel exports (logos, banners, footers, blank gutters, subtotals,
transposed layouts, multi-sheet workbooks) into flat, table-ready CSVs — with an audit
report explaining every structural decision.

No per-file configuration. Every choice is made from cell content, so the pipeline
works on files it has not seen.

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
at other files, or elsewhere:

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

## How it decides

**Orientation.** The canonical schema is known ahead of time, so the axis whose leading
lines read as field names is the field axis. File4's column 1 matches 8/8; its row 1
matches 1/11. Type homogeneity, computed on a fine lattice that separates `POL-1000001`
from `Metro Agency Group`, only breaks near-ties.

**Header band.** The row with the highest alias match rate, scanning a band — not the
first populated row (File2's banner would win) and not the first gapless row (File1's
header has an empty gutter at F2). Empty cells are excluded from the match denominator
so a header with a hole is not penalised.

**Junk rows.** Classified by content across the whole body, because File3's subtotals sit
mid-table. A total row must both name itself in its first cell *and* leave the
record-identifying columns empty, so a profit centre legitimately called "Total Risk PC"
survives. A single blank row is a cosmetic separator (File1 row 8), not a terminator.

**Join keys.** Found from values, never header names — `producer_agency_name` and
`producer` do not string-match. A cheap normalized exact-overlap pass settles most cases;
fuzzy scoring, which is quadratic, only runs on what is left.

**Fuzzy resolution has a margin rule.** `token_set_ratio` returns 100 whenever one side's
tokens are a subset of the other's, so a generic value clears any absolute threshold
against several candidates at once — `Agency Group` scores 100 against both
`Metro Agency Group` and `MJC Agency Group`. A match auto-resolves only if it clears 90
**and** beats the runner-up by 10. `MJC ` passes (100 vs 10); `Agency Group` is flagged
for a human instead of silently guessed.

**Joins are always left.** A fact row never disappears because its lookup missed; the
dimension columns come back null and the value is listed for review.

**Types are decided, not inherited.** `profit_center_number` is always a string (it
arrives as `1005` in one file and `PC0001` in another). Dates normalise to ISO from both
text and real datetimes. `commission_pct` stays a whole-number percent. ZIP codes are
zero-padded to five, restoring the leading zero Excel dropped from `08085`.

## Verification

The junk the pipeline strips is the oracle for the data it keeps. File2's discarded grand
total (`230,712.19`) and File3's three discarded subtotals must equal the sums of the
cleaned rows — asserted in `tests/test_extract.py`, and all four reconcile exactly.

## Layout

```
clean.py            entry point (no PYTHONPATH needed)
src/ahi_clean/
  schema.py         canonical fields, aliases, target dtypes
  typing_utils.py   fine-grained type inference
  reader.py         workbook -> cell grid (nulls Excel error cells)
  extract.py        one sheet -> tidy DataFrame + trace
  orchestrate.py    sheet roles, stack/join decisions
  join.py           key discovery + fuzzy resolution with the margin rule
  audit.py          trace -> JSON report
  cli.py            python -m ahi_clean
```
