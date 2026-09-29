# AHI POC — Schema-free Excel cleaning pipeline

Turns report-shaped Excel exports and delimited text files — banners, merged titles, footers, page breaks, blank
gutters, subtotals, repeated headers, transposed layouts, side-by-side tables — into flat,
table-ready CSVs, with an audit report explaining every structural decision and a
per-column metadata summary beside every cleaned file.

**No schema, no aliases, no field names.** The pipeline has no idea what a "premium" is.
Every decision comes from the data's own shape, types, distinctness, density and
arithmetic, so a file with column names it has never seen goes through the same code path
as a known one. Output columns are the file's own labels, normalized.

**Every scenario workbook produces the correct records.** A few carry an advisory — the
pipeline reporting honest uncertainty, not an error.

Built on **polars**. **1,000,000 rows in 93 seconds** on one sheet. A bad workbook costs
you that workbook and nothing else.

- [BUSINESS_GUIDE.md](BUSINESS_GUIDE.md) — how it decides, in plain language, no code
- [ARCHITECTURE.md](ARCHITECTURE.md) — the full design, diagrams and the ablation study

## Run

```bash
pip install -r requirements.txt
```

Run from the project root. `clean.py` puts `backend/src/` on the path itself, so nothing
needs setting first — this works the same in PowerShell, cmd and bash.

```bash
python clean.py
```

With no arguments it cleans every `.xlsx`, `.csv` and `.tsv` in
`backend/sample_files_uncleaned/` into `backend/cleaned/` and `backend/audit/`. That folder
is git-ignored — the scenario corpus is expected to be swapped, so supply your own files
there. (`backend/legacy/` keeps the six original POC workbooks, which are tracked.) Paths
you pass are relative to where you run the command. To point it at other files:

```bash
python clean.py "some_folder/*.xlsx" --out cleaned --audit audit
```

`--workers 0` uses one worker per CPU. `--max-cells` sets the per-sheet ceiling (default
20,000,000). Process several workbooks at once:

```bash
python clean.py "inbox/*.xlsx" --workers 8
```

Score the cleaner against every scenario workbook:

```bash
python backend/tools/scorecard.py
```

Run the tests (from the project root; `pytest.ini` points at `backend/tests`):

```bash
python -m pytest
```

Measure it, and check which signals the current corpus actually exercises:

```bash
python backend/tools/benchmark.py
```

```bash
python backend/tools/ablation.py
```

## Web app

A browser UI over the same cleaner: upload a workbook, pick sheets, run, review, rename
headers, export. The API (`backend/api/`) calls the `ahi_clean` functions in
`backend/src/` directly and changes none of them. Run from the project root:

```bash
.venv\Scripts\python -m uvicorn api.main:app --app-dir backend --reload --port 8000
```

```bash
npm --prefix frontend install
```

```bash
npm --prefix frontend run dev
```

Open http://localhost:5173 (Vite proxies `/api` to port 8000). For a single-origin
deployment, `npm --prefix frontend run build` and uvicorn then serves `frontend/dist` itself.

What the UI can and cannot know, and when:

- **Before a run** only sheet names are shown. A dirty sheet's raw extent — banners,
  subtotals, blank rows — says nothing reliable about the table inside it, so row and
  column counts are left to the cleaner.
- **Appending** is a setting, not a prediction. With it on, sheets whose *cleaned*
  headers match exactly are stacked (`orchestrate.plan_workbook`); with it off, each table
  ships alone. Which sheets actually combined is reported after the run.
- **Header renames** are stored per output and applied at export time, so a CSV or
  metadata file downloaded after a rename always carries the new names. The metadata is
  still built from the original frame, so appended tables stay described per sheet.

Uploads, job outputs and exports go to `backend/.workspace/` (git-ignored). Jobs live in memory,
so a server restart means uploading again.

| Endpoint | Purpose |
|---|---|
| `POST /api/workbooks` | Upload; validates type, size and readability; lists sheets |
| `POST /api/jobs` | Start cleaning `{workbook_id, sheets, append}` |
| `GET /api/jobs/{id}` | Live status: stage, current sheet, rows kept/removed |
| `POST /api/jobs/{id}/cancel` | Stop after the current sheet |
| `GET /api/jobs/{id}/results` | Outputs, per-sheet report, append decisions, warnings |
| `GET /api/jobs/{id}/outputs/{oid}/preview` | Paginated rows, server-side search/sort |
| `GET /api/jobs/{id}/outputs/{oid}/columns` | Column profile (the metadata) |
| `PUT /api/jobs/{id}/outputs/{oid}/headers` | Rename headers `{renames: {old: new}}` |
| `GET /api/jobs/{id}/outputs/{oid}/export/csv` · `/metadata` | Downloads with current headers |
| `GET /api/jobs/{id}/export/audit` · `/export/zip` | Audit report; everything zipped |

## Outputs

Each run writes three kinds of file:

| File | Where | What |
|---|---|---|
| `<source>_<job_id>.csv` | `--out` | a cleaned table |
| `<source>_metadata_<job_id>.csv` | `--out` | one row per column of that table (below) |
| `<source>.audit.json` | `--audit` | every structural decision, with its score and reason |

`<job_id>` is a fresh UUID4, fixed when the file is written and shared by a cleaned CSV and
its metadata file. `<source>` is the input file's stem. A workbook that yields several
tables produces one CSV and one metadata file per table, so n sheets with different
headers give n CSVs and n metadata files. The audit lists each output under its final
names (`file`, `metadata_file`, `job_id`, plus `table`, the cleaner's own name for it).
`python clean.py` and `python -m ahi_clean` (with `backend/src` on the path) produce exactly
the same files.

### The metadata file

Built from the typed table at the moment it is written, so it never re-guesses types:

| Column | Meaning |
|---|---|
| `header_name` | the cleaned column name |
| `datatype` | the type the cleaner decided (`Int64`, `Float64`, `Date`, `String`, ...) |
| `inferred_datatype` | what polars infers from the written CSV's first 1,000 rows (`infer_schema_length=1000`) |
| `distinct_count` | distinct non-empty values |
| `count` | non-empty values |
| `total_row_count` | rows in the table, or in this sheet's block |
| `min`, `max` | numeric and date columns; `NA` otherwise |
| `sum` | numeric columns, summed exactly as decimals; `NA` otherwise |
| `null_percentage` | empty cells as a % of rows, any type, 2 decimals |
| `excel_name` | the source file |
| `sheet_name` | the sheet the rows came from |
| `type_flag` | every edge case on the column (below), joined with ` \| `; `NA` when there is none |

A statistic that does not apply is written as `NA`: `min`/`max`/`sum` of a text column,
`sum` of a date column, or any of them for a column with no values. An appended table gets one
block of rows per sheet, each labelled with its `sheet_name`.

**How codes and numbers are told apart**, from values only, never names:

- **any value mixing letters and digits** (`12AB`, `A7`) → the column is text.
- **a value with a leading zero** (`08085`) → a code, kept as text.
- **same-width, all-different whole numbers** (`1005, 1006, 1007`) could be codes (ZIPs,
  account or centre numbers) or amounts; the values cannot say which. **More than 10
  values** → read as a code (`String`); **10 or fewer** → read as a number (`Int64`).
  Either way the column is **flagged** in the metadata's `type_flag`, so someone can
  check it against its header.

### `type_flag`: every edge case, flagged

The last metadata column lists everything about a column a reader should know. Each flag
starts with its kind:

- **`CHECK`**: the cleaner guessed, or had to leave values empty. Look at the column
  (usually its header) and confirm.
- **`INFO`**: the cleaner changed something on purpose and was sure. Nothing to decide.

Several flags on one column are joined with ` | `. On an appended table each flag is
prefixed with its sheet (`Jan: CHECK: ...`), because sheets can differ.

| Flag | Kind | When |
|---|---|---|
| same-width, all-different whole numbers; read as code / as number | CHECK | could be ZIPs, account numbers or amounts; more than 10 values → code, 10 or fewer → number |
| every value is a 5-digit number; read as dates because the header names a date | CHECK | ZIP or Excel serial? the header (`Txn Dt`, `created_at`, ...) broke the tie |
| every value is a 5-digit number; the header does not name a date, so kept as number | CHECK | could be ZIP codes, amounts or Excel date serials |
| every value is an 8-digit valid yyyyMMdd date | CHECK | read as dates, but could be identifiers |
| mostly numbers, but N value(s) mix letters and digits | CHECK | whole column kept as text |
| mixes identifiers (like POL-1) with plain numbers | CHECK | whole column kept as text |
| N value(s) are not numbers and were left empty | CHECK | e.g. `N/A` in a number column |
| N value(s) could not be read as dates and were left empty | CHECK | stragglers in a date column |
| looked like dates, but none could be read | CHECK | kept as text |
| dates such as 01/02/2026 fit both month-first and day-first | CHECK | nothing in the column decided the order |
| percent signs removed: 99% is stored as 99 | CHECK | not 0.99 |
| no header row was found / the header cell was blank | CHECK | column named by position (`column_3`) |
| the header appears more than once; this copy renamed `x_2` | CHECK | duplicate labels |
| column names borrowed from another sheet | CHECK | a continuation sheet with no header |
| which way up this table is was a close call | CHECK | decided by shape; check it is not sideways |
| entirely empty: formulas with no saved results | CHECK | open and re-save the workbook in Excel |
| numbers with leading zeros; kept as text | INFO | e.g. `08085` |
| dates read as day-first / month-first | INFO | only that reading fits every value |
| dates written in several formats; normalised to YYYY-MM-DD | INFO | |
| N Excel serial number(s) converted to dates | INFO | e.g. `46030 = 2026-01-08` |
| N value(s) written as yyyyMMdd read as dates | INFO | inside a date column |
| time of day dropped from N value(s) | INFO | `2026-01-08 10:30:00` → `2026-01-08` |
| formatted numbers; removed currency symbols, separators, brackets | INFO | `(500)` = -500 |
| European number format: comma is the decimal mark | INFO | `1.234,56` read as 1234.56, `99,5` as 99.5; decided per column from the values |
| thousands separators removed | INFO | `1,234.56` read as 1234.56 |
| values such as 1,234 fit both number formats; read as US | CHECK | `1,234` is 1234 (US) or 1.234 (European); nothing in the column decides |
| mixes US and European number formats | CHECK | read by the majority; the other values may be wrong |
| N value(s) with a trailing minus read as negative | INFO | `500-` = -500 |
| N value(s) in scientific notation | CHECK | `1.23E+05`: Excel may already have rounded off an ID's digits |
| about N% of the values read as dates / numbers, too few; kept as text | CHECK | a partly mixed column (20–60%) |
| 8-digit numbers kept as text because the header names an identifier | CHECK | `20240101` under `PolicyNo`; under a date header or neither, read as dates, CHECK |
| the header was the number 2024; named `col_2024` | INFO | |
| the header spanned two rows; the labels were joined | INFO | |
| values were moved back under their labels | INFO | header and data had different gaps |
| the table was sideways; it was turned upright | INFO | |
| created by unpivoting N columns / the cells of the unpivoted columns | INFO | on the new `period`/`category` and `value` columns |
| added by the cleaner: the sheet each row came from | INFO | `source_sheet` on appended tables |
| entirely empty in the source | INFO | |
| the separator was a close call | CHECK | CSV: two separators split the lines equally well |
| the file is not UTF-8; read as Windows-1252 | INFO | CSV |
| read as Latin-1, so accented or special characters may be wrong | CHECK | CSV: last-resort encoding |
| N Excel error value(s) (#REF!, #N/A ...) were left empty | CHECK | lists the cells |
| N formula cell(s) have no saved result and are empty in the output | CHECK | open and re-save in Excel |
| N merged cell range(s) spanning several rows were filled in every row | INFO | `A5:A7`; title merges across one row are not flagged |
| the header row was chosen with low confidence | CHECK | header score under 0.65 (cut-off 0.55) |
| the header and the data could not be lined up | CHECK | each side has columns the other lacks |
| table boundary was a close call | CHECK | rows joined or split across a gap, or a blank column treated as a gap or a split, within 10% of the threshold |
| N sheets with different names matched equally well | CHECK | a continuation sheet with two possible donors |
| N row(s) kept although they look incomplete | CHECK | sparse rows that are not totals; lists the sheet rows |
| N row(s) labelled as totals were dropped although their figures did not add up | CHECK | lists the sheet rows |

Flags about the file, the sheet, the table and its rows are not about any one column,
so they are attached to every column of the table they affect.

**`datatype` is the schema to load with**, after checking any flagged column.
`inferred_datatype` shows what a loader that guesses types would get instead. Where the
two differ, typically a code column inferred as `Int64`, loading with inference would
drop leading zeros or add up identifiers. Pass `datatype` as an explicit schema.

## Delimited files

`.csv`, `.tsv` and `.txt` go through the same pipeline. A delimited file is one sheet by
definition, and it produces the same grid of cells, so every structural rule below applies
unchanged — banners, footers, subtotals, transposed layouts and all.

The delimiter is **detected, not assumed**: whichever of comma, semicolon, tab or pipe
yields the most *consistent* column count across the opening lines. Frequency alone picks
the comma out of a semicolon-delimited file full of prose. Encoding is tried in order
(UTF-8 with or without a BOM, then cp1252, then latin-1), so a file always opens rather
than failing on a bad guess.

What a CSV cannot carry: cell types, formatting, merged ranges, formulas. That costs less
than it sounds — the type lattice already classifies by *shape*, the emphasis signal is
additive, and a merged title arrives as one value followed by empties, which is exactly
what the banner rule looks for. One thing a CSV does **better**: a leading zero survives,
where Excel had already destroyed it before the pipeline saw the file.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | every workbook cleaned, no contract violated |
| 1 | no input matched |
| 2 | some workbooks could not be read, or an output file was locked (usually open in Excel) |
| 3 | an output contract was violated — the data may be wrong |

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

**Multiple sheets.** Every table ships as its own CSV. Tables are appended only when their
column headers match 100% (same set of names, any order), with a `source_sheet` column
added; nothing partial counts. Tables are never joined.

**Types.** Inferred per column. Alphanumeric values and leading zeros stay text;
same-width, all-different whole numbers are a code above 10 values and a number at 10 or
fewer, and flagged either way; a column in four different date formats is recognised by what parses, not by how it
looks; formula cells with no cached value are reported rather than shipped as nulls.

**Dates are written as `YYYY-MM-DD`.** Every value in a date column is normalised,
whichever of these it arrived in: `yyyy-MM-dd HH:mm:ss` (time dropped), `yyyy-MM-dd`,
`MM/dd/yyyy`, `M/d/yyyy`, `yyyyMMdd`, `MM-dd-yyyy`, `M-d-yyyy`, the written forms
(`08 Jan 2026`, `Jan 8, 2026`, `08.01.2026`), day-first slashes when the column proves it,
and five-digit Excel serial numbers (`46030` → `2026-01-08`, i.e. 1899-12-30 plus n days).
Two numeric guards: a column of nothing but numbers is a date column only if *every* value
is a real `yyyyMMdd` date in 1900–2100, and a serial is only read as a date in a column
that already holds dates written some other way. A column of *only* 5-digit numbers is
settled by its header (below).

**The one place a header is read: ZIP code or Excel date serial.** A column of nothing
but 5-digit numbers that are all plausible serials (years 1900–2100) is a genuine tie:
`46030` is an Indiana ZIP code and also `2026-01-08`. The values cannot settle it, so the
column's header does, and the column is flagged `CHECK` either way:

- the header names a date → read as Excel serials and converted to `YYYY-MM-DD`;
- otherwise → kept as a number.

The header counts as a date in three ways (`coerce.header_names_a_date`):

| Match | Words | Examples that match |
|---|---|---|
| **anywhere**, like SQL `LIKE '%date%'` | `date`, `dt`, `time`, `day`, `period`, `expir`, `effectiv`, `matur`, `birth`, `fecha`, `datum`, `giorno`, `tarih` | `TxnDate`, `txndt`, `TXN_DTTM`, `Timestamp`, `PeriodEnd`, `ExpiryDt` |
| **whole word only**, after splitting on separators and camelCase | `at`, `on`, `ts`, `dob`, `doj`, `when`, `since`, `until`, `due`, `asof`, `eff`, `tag`, `dia`, `jour` | `created_at`, `CreatedAt`, `Posted On`, `updated_ts`, `DOB` |
| **event word**, unless a person or id is named beside it | `created`, `updated`, `modified`, `posted`, `issued`, `closed`, `opened` | `LastUpdated`, `Closed`; but not `created_by` or `updated_by`, which hold user ids |

The words follow common naming conventions: dbt and GitLab use `<event>_at` for
timestamps and `<event>_date` for dates, Oracle uses `_dt` and `_dttm`, `_on` marks
date-only columns, and `_ts` marks timestamps.

**Ordinary words containing a date fragment are set aside first.** Otherwise `width`
would match `%dt%`, `lifetime` `%time%`, and `update_count` or `candidate` `%date%`. The
list is `NOT_DATE_FRAGMENTS` in `coerce.py`.

**Why `at`, `on` and `ts` are whole words and not `%at%`.** As a substring, `at` matches
`rate`, `state`, `status`, `category`, `format`, `vat`, `latitude` and `location`. That
would turn a ZIP column under a `State` header into dates. The short words are matched
only when they stand alone, so they still catch `created_at`, `CreatedAt` and
`Posted On`. The same applies to `dia` (`media`, `india`), `tag` (`stage`) and `jour`
(`journal`).

**Continuation sheets.** A sheet with no header at all adopts the column names of a sibling
with the same width and the same per-column types — matched on shape, not on sheet order.

## Robustness

**A bad file costs you that file.** Every workbook is processed inside its own error
boundary, so one corrupt workbook in a folder of five hundred no longer takes the run
down and skips everything after it. Failures are classified into things you can act on —
corrupt, password-protected, unsupported format, missing, too large — each with advice,
and an unexpected error keeps its traceback in a file rather than spilling into the
console.

**Output contracts.** The pipeline refuses to look trustworthy when it is not. Before
anything is written it checks that every row is accounted for as kept or
dropped-with-a-reason, that no key column came out empty, that no column emptied without
a stated cause, and that a removed grand total still reconciles with the rows that were
kept. A violation writes the CSV — withholding it would hide the evidence — and fails the
run with its own exit code.

**A documented ceiling.** `--max-cells` refuses a sheet too large to attempt rather than
hanging. The reader also trims the sheet to its genuinely populated rectangle first, so a
stray styled cell at row 100,000 does not make a ten-row report allocate a dense grid of
a hundred thousand.

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

**Performance, measured not claimed.** 1,000,000 rows in 93 seconds, from ~34 minutes
before the polars migration — 22x. The win came from deciding a column's type once and
applying it vectorised, rather than calling a scalar parser per cell: profiling showed 51%
of the old runtime inside pandas' date parser, more than half of that re-guessing the
format on every single call.

**Parallelism, defaulted on measurement.** Threads, not processes. Over 24 workbooks, four
threads ran 1.4x faster than sequential while four processes ran **3.6x slower** — on
Windows each process is a fresh interpreter that must re-import polars before doing any
work, and for report-sized files that startup dwarfs the job. `--executor process` remains
available for batches of genuinely large files.

**170 tests with an empty corpus, in two suites.** `backend/tests/test_scenarios.py` runs against
whatever workbooks are in `backend/sample_files_uncleaned/` and adds tests per workbook
automatically. With that folder empty, the 16 tests that need real workbooks (there and in
the orchestration and resilience suites) are skipped rather than failed. Everything else builds its sheets in
memory and never names a file, so swapping the corpus cannot break what they pin.

## Layout

```
clean.py                 entry point (no PYTHONPATH needed)
requirements.txt         Python dependencies (cleaner + API)
pytest.ini               runs backend/tests from the project root
frontend/                React + Vite UI (Configuration → Run → Review & Results)
backend/
  api/                   FastAPI service: routes, validation, jobs, exports
  legacy/                the original six POC workbooks
  sample_files_uncleaned/  the scenario corpus (git-ignored; supply your own)
  tests/                 cleaner and API test suites
  tools/scorecard.py     scores the cleaner against every scenario workbook
  tools/benchmark.py     single-sheet throughput and executor comparison
  tools/ablation.py      disables each signal in turn to show what is load-bearing
  src/ahi_clean/
    signals.py        scoring primitives (fill, uniqueness, type profile, coverage, contrast)
    typing_utils.py   fine-grained type inference
    reader.py         workbook -> cell grid + formatting + formulas; nulls error cells
    delimited.py      csv/tsv -> the same grid; sniffs delimiter and encoding
    geometry.py       sheet -> table regions; all blank-gap handling
    header.py         header scoring, the no-header path, column naming
    rowclass.py       sparsity + arithmetic row classification
    pivot.py          wide-matrix detection and the melt to long form
    coerce.py         vectorised type decisions: one strategy per column, not per cell
    contracts.py      checks that refuse, rather than merely report
    failures.py       why a workbook could not be read, in actionable terms
    extract.py        orientation, regions, realignment, types, validation
    orchestrate.py    table roles, exact-header appends, one CSV per table otherwise
    metadata.py       per-column metadata, from the typed table as it is written
    audit.py          trace -> JSON report
    cli.py            batch isolation, parallel workers, CSV + metadata writing, job ids
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
- Output file names carry the source stem and a job ID, not the sheet; the sheet is in the
  metadata's `sheet_name` and the audit's `source_sheets`.
- The decimal mark is decided per column from the values (`1.234,56` or `99,5` settles
  European; `1,234.56` or `10.5` settles US). A column whose values fit both, such as
  `1,234` only, is read as US and flagged.
- `backend/tools/scorecard.py` scores `.xlsx` only; delimited files are covered by the tests.
