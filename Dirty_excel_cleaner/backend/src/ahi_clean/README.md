# ahi_clean — command line

Turns report-shaped Excel and text exports into table-ready CSVs. Every CSV gets a
metadata file beside it, and every input gets an audit report explaining each decision.
How it decides is in [ARCHITECTURE.md](ARCHITECTURE.md).

Inputs: `.xlsx` `.xlsm` `.xls` `.xlsb` `.csv` `.tsv` `.txt` (`.xls` needs `xlrd`,
`.xlsb` needs `python-calamine`; both are in `requirements.txt`).

## Run

From the project root (no `PYTHONPATH` needed):

```bash
python clean.py
```

Or as a module, with `backend/src` on the path:

```bash
python -m ahi_clean <inputs> [options]
```

## Commands

| Goal | Command |
|---|---|
| Clean the sample folder | `python clean.py` |
| Clean one file | `python clean.py "Test_data/2026 07 JUL/PC0152_Carrier Totals by Month Jan-July 2026_08132026.xlsx"` |
| Clean a folder | `python clean.py "Test_data/2026 07 JUL"` |
| Clean a folder tree | `python clean.py Test_data -r` |
| Glob pattern | `python clean.py "inbox/*.xlsx" "inbox/*.csv"` |
| Skip files by name | `python clean.py Test_data -r --exclude "*donotingest*"` |
| Only some sheets | `python clean.py book.xlsx --sheet "July" --sheet "August"` |
| Never append tables | `python clean.py book.xlsx --no-append` |
| See decisions, write nothing | `python clean.py Test_data -r --dry-run` |
| Choose output folders | `python clean.py inbox --out cleaned --audit audit` |
| Parallel, one worker per CPU | `python clean.py inbox --workers 0` |
| Large files in processes | `python clean.py big/ --workers 4 --executor process` |
| Raise the size ceiling | `python clean.py huge.xlsx --max-cells 50000000` |

## Options

| Option | Default | Meaning |
|---|---|---|
| `inputs` | `sample_files_uncleaned` | Files, folders or glob patterns. A folder contributes every supported file in it. |
| `-r`, `--recursive` | off | Also search sub-folders; `**` in a pattern matches folders. |
| `--exclude PATTERN` | none | Skip files whose name matches (case-insensitive, repeatable). |
| `--sheet NAME` | every sheet | Clean only the named sheet(s) of each workbook (repeatable). |
| `--no-append` | append on | Ship every table alone, even when two tables' headers match exactly. |
| `--dry-run` | off | Read, decide and check everything. Print each table's shape, orientation, header and dropped rows. Write nothing. |
| `--out DIR` | `backend/cleaned` | Where CSVs and metadata files go. |
| `--audit DIR` | `backend/audit` | Where audit reports (`*.audit.json`) go. |
| `--workers N` | 1 | Files cleaned at once. `0` = one per CPU. |
| `--executor` | `thread` | `thread` (faster for report-sized files) or `process` (big batches of large files). |
| `--max-cells N` | 20,000,000 | Refuse a sheet larger than this rather than risk running out of memory. |

Excel lock files (`~$Book.xlsx`) are always skipped. A file you name explicitly is
attempted even if its type is unsupported, so it is reported, not silently ignored.

## Outputs

For each input `Book.xlsx`:

| File | What |
|---|---|
| `cleaned/<table>_<job_id>.csv` | One per table. Tables with identical headers are appended into one. |
| `cleaned/<table>_metadata_<job_id>.csv` | Per column: type, distinct, nulls, min/max/sum, flags. |
| `audit/Book.audit.json` | Every decision: orientation, regions, header scores, dropped rows with reasons, coercions, contract checks. |
| `audit/Book.error.txt` | Only for an unexpected failure: the traceback. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything cleaned, every contract held. |
| 1 | No input matched. |
| 2 | A file could not be read (corrupt, encrypted, unsupported, too large) or written (open in Excel). The others are still cleaned. |
| 3 | An output contract was violated. The CSV is still written so the problem can be inspected; see stderr and the audit. |

## Checking the cleaner itself

```bash
python -m pytest
```

```bash
python backend/tools/snapshot_sheets.py snap Test_data/2026\ 07\ JUL before.json
```

```bash
python backend/tools/snapshot_sheets.py diff before.json after.json
```
