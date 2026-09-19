# AHI POC — Schema-free Excel cleaning pipeline

Turns report-shaped Excel exports — banners, merged titles, footers, page breaks, blank
gutters, subtotals, repeated headers, transposed layouts, side-by-side tables — into flat,
table-ready CSVs, with an audit report explaining every structural decision.

**No schema, no aliases, no field names.** The pipeline has no idea what a "premium" is.
Every decision comes from the data's own shape, types, distinctness, density and
arithmetic, so a file with column names it has never seen goes through the same code path
as a known one. Output columns are the file's own labels, normalized.

**Every scenario workbook produces the correct records.** A few carry an advisory — the
pipeline reporting honest uncertainty, not an error.

- [BUSINESS_GUIDE.md](BUSINESS_GUIDE.md) — how it decides, in plain language, no code
- [ARCHITECTURE.md](ARCHITECTURE.md) — the full design, diagrams and the ablation study

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

Score the cleaner against every scenario workbook:

```bash
python tools/scorecard.py
```

Run the tests:

```bash
python -m pytest
```

## How it decides, in one line each

**Pivots, first of all.** A month-per-column matrix reads consistently in *both* directions,
so orientation would flip it on a tiebreak. It is recognised before that happens — by a run
of same-typed, all-distinct headings over a single numeric measure — and melted into long
form, one row per cell.

**Sheet orientation, next.** Each column of an upright table holds one type and its header
labels are distinct; a transposed table reverses both. This is settled *before* the sheet is
cut into regions — in a sideways sheet a blank row is a blank field, not a table separator.

**Regions.** A sheet may hold several tables. Row gaps are judged by whether both sides use
the same columns (*coverage*) and hold the same kinds of value (*type agreement*). **Gap
size is never an input** — one blank row and twelve behave identically.

**Header.** Scored on type contrast with the columns below, textness, uniqueness, fill and
brevity, plus bold/fill/border as a bonus. Below 0.55 the pipeline **refuses to pick one**
and names columns positionally, because naming a data row as the header is the worse failure.

**Alignment.** When the header and the data disagree about where the gutters are, the body
is re-seated under the header rather than left to land under the wrong labels.

**Junk rows.** Sparsity finds candidates; **arithmetic confirms them** — a total is a row
whose number equals the sum of the rows above it. A repeated header is caught three ways:
verbatim, as a fragment, or restated in different words (text sitting where its columns hold
numbers).

**Join keys.** Found from values, never names — without a schema the two columns are
literally called `producer_agencyname` and `producer`. Fuzzy matches auto-resolve only if
they clear 90 **and** beat the runner-up by 10.

**Types.** Inferred per column. Leading zeros and constant-width near-unique integers stay
text; a column in four different date formats is recognised by what parses, not by how it
looks; formula cells with no cached value are reported rather than shipped as nulls.

**Continuation sheets.** A sheet with no header at all adopts the column names of a sibling
with the same width and the same per-column types — matched on shape, not on sheet order.

## Two rules that keep it safe

**A sparse row that fails the arithmetic and carries no total-ish wording is kept**, flagged
at low confidence. A record with several empty fields is ordinary in real data, and silently
losing one is the worst failure available.

**Wording alone can never drop a row.** The row must be sparse on structure first; the
wording only decides which kind of non-record it is.

## One deliberate concession, stated plainly

Real reports carry totals whose figures are stale or hand-edited — one test file states a
total of `48,387.01` against rows summing to `48,383.11`. Keeping those as data
corrupts every downstream sum, so a row that is *both* sparse and labelled a total is
dropped into its own `UNVERIFIED_TOTAL` class and flagged for review.

The cost is measured, not assumed: deleting every text pattern now breaks the files that
carry a wrong total. In an earlier version it changed nothing at all. That is a trade, and
the ablation table in [ARCHITECTURE.md §9](ARCHITECTURE.md#9-ablation-what-is-actually-load-bearing)
shows exactly what it bought.

## Verification

**Objective oracle.** Every genuine record in the corpus carries a `POL-nnnnnn` policy
number; banners, totals, footers and repeated headers do not. Counting those tokens in the
raw grid gives an expected record count that owes nothing to the code being tested.

For a pivot, which carries no policy numbers, conservation is the oracle instead: unfolding
must produce exactly one row per value cell. A workbook the oracle cannot check at all is
**reported as unchecked**, never scored as a pass.

**Ablation.** Every signal is disabled in turn and re-scored, so no assumption can quietly
become load-bearing without showing up. The harness has itself been wrong three times —
each time an ablation "passed" because the scorecard could not see the damage, and each gap
is now closed with a direct check. A benchmark that cannot fail is not measuring anything.

**169 tests, in two suites.** `tests/test_scenarios.py` runs against whatever workbooks are
in `sample_files_uncleaned/` and adapts automatically. Everything else builds its sheets in
memory and never names a file, so swapping the corpus cannot break what they pin.

## Layout

```
clean.py            entry point (no PYTHONPATH needed)
tools/scorecard.py  scores the cleaner against every scenario workbook
src/ahi_clean/
  signals.py        scoring primitives (fill, uniqueness, type profile, coverage, contrast)
  typing_utils.py   fine-grained type inference
  reader.py         workbook -> cell grid + formatting + formulas; nulls error cells
  geometry.py       sheet -> table regions; all blank-gap handling
  header.py         header scoring, the no-header path, column naming
  rowclass.py       sparsity + arithmetic row classification
  pivot.py          wide-matrix detection and the melt to long form
  extract.py        orientation, regions, realignment, types, validation
  orchestrate.py    table roles, stack/join decisions
  join.py           value-based key discovery with the margin rule
  audit.py          trace -> JSON report
```

## Known limits

- **Semantic mapping is out of scope by construction.** Deciding that `Producer` in one file
  and `Agent Name` in another belong in the same target column needs a schema, a config, an
  LLM or a human. The pipeline stops at per-file-correct structure and records every
  inferred type.
- A leading zero already destroyed in the source (a ZIP stored as the number `8085`) is
  unrecoverable.
- Orientation needs ≥2 data rows; near-square tables fall back to a shape tiebreak and are
  flagged `confident: false`.
- A gutter ≥2 columns wide inside one table is read as a table boundary unless both sides
  span exactly the same rows.
- Pivot detection needs at least three value columns; a two-month matrix is indistinguishable
  from an ordinary table with two numeric columns.
