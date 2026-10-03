# AHI POC — New Developer Onboarding Guide

Welcome to the AHI POC (Proof of Concept) repository. This guide will get you productive on day one.

---

## What This Project Does

**AHI POC** is a schema-free Excel cleaning pipeline that transforms messy, report-shaped Excel exports and delimited text files into clean, table-ready CSVs — complete with per-column metadata and an audit report explaining every structural decision.

### Key Capabilities
- **No schema required** — works on files with columns it has never seen
- **Handles real-world messiness** — banners, merged titles, footers, page breaks, blank gutters, subtotals, repeated headers, transposed layouts, side-by-side tables, pivot matrices
- **Audit trail** — every decision recorded with score and reason
- **Per-column metadata** — type, distinct count, null %, min/max/sum, edge-case flags
- **Bronze/Silver ingestion** — loads cleaned data into Postgres with human-in-the-loop approval
- **Web UI** — upload, clean, review, rename headers, export, ingest to bronze, map to silver

### Performance
- **1,000,000 rows in 93 seconds** on a single sheet (built on Polars)
- Thread-based parallelism (processes are slower on Windows due to interpreter startup)

---

## Repository Structure

```
AHI_POC/
├── clean.py                    # CLI entry point (no PYTHONPATH needed)
├── requirements.txt            # Python deps (cleaner + API)
├── pytest.ini                  # Runs backend/tests from project root
├── .env.example                # Environment template (copy to backend/.env)
├── README.md                   # Project overview & run commands
├── BUSINESS_GUIDE.md           # Plain-language explanation of cleaning logic
├── ARCHITECTURE.md             # Technical reference with diagrams
├── ONBOARDING.md               # This file
├── assets/                     # Business reference workbooks (division mapping, LOTL, DRT, Silver schemas)
├── frontend/                   # React + Vite + TypeScript UI
│   ├── src/
│   │   ├── pages/              # Configure, Run, Results, Ingest, Silver, Users, History
│   │   ├── state/              # React state (workflow, auth)
│   │   └── lib/                # Hooks, formatting utilities
│   └── package.json
└── backend/
    ├── api/                    # FastAPI service
    │   ├── main.py             # App factory, routers, lifespan
    │   ├── routes/             # API endpoints (workbooks, jobs, exports, bronze, silver, users, history)
    │   ├── services/           # Business logic (job_history, bronze_service, silver_service, reference_service)
    │   ├── db.py               # Bronze DB pool
    │   ├── app_db.py           # App DB pool (users, job history)
    │   ├── auth.py             # Session auth (PBKDF2-SHA256, HMAC-SHA256 cookies)
    │   ├── config.py           # Settings from env
    │   ├── schemas.py          # Pydantic models
    │   └── errors.py           # Error handling
    ├── migrations/             # SQL for bronze control tables (ingest.bronze_table, ingest.ingestion, ingest.ingest_plan)
    ├── migrations_app/         # SQL for app tables (users, jobs)
    ├── legacy/                 # Original 6 POC workbooks (tracked)
    ├── sample_files_uncleaned/ # Scenario corpus (git-ignored; supply your own)
    ├── tests/                  # 170+ tests (see Testing section)
    ├── tools/
    │   ├── scorecard.py        # Scores cleaner against scenario workbooks
    │   ├── benchmark.py        # Single-sheet throughput & executor comparison
    │   ├── ablation.py         # Disables each signal to show load-bearing ones
    │   ├── create_user.py      # Creates first admin user
    │   ├── seed_reference.py   # Loads reference tables from assets/
    │   └── make_silver_test_files.py
    ├── config/
    │   ├── silver_columns.csv          # 69 silver_detail columns
    │   └── silver_aggregate_columns.csv # 75 silver_aggregate columns
    └── src/
        ├── ahi_clean/          # Core cleaning pipeline (see ARCHITECTURE.md §1)
        │   ├── signals.py      # Scoring primitives (fill, uniqueness, type profile, coverage, contrast)
        │   ├── typing_utils.py # Fine-grained type inference
        │   ├── reader.py       # Workbook → cell grid + formatting + formulas
        │   ├── delimited.py    # CSV/TSV → same grid; sniffs delimiter & encoding
        │   ├── geometry.py     # Sheet → table regions; blank-gap handling
        │   ├── header.py       # Composite header scoring, no-header path, column naming
        │   ├── rowclass.py     # Sparsity + arithmetic row classification
        │   ├── pivot.py        # Wide-matrix detection & melt to long form
        │   ├── coerce.py       # Vectorised type decisions (one strategy per column)
        │   ├── contracts.py    # Output contracts that refuse (not just report)
        │   ├── failures.py     # Actionable failure classification
        │   ├── extract.py      # Orientation, regions, realignment, types, validation
        │   ├── orchestrate.py  # Table roles, exact-header appends, output per table
        │   ├── metadata.py     # Per-column metadata from typed frame
        │   ├── audit.py        # Trace → JSON report
        │   └── cli.py          # Batch isolation, parallel workers, CSV + metadata writing
        ├── ahi_bronze/         # File → Bronze rules (pure, unit-tested)
        │   ├── naming.py       # Table names, source system, reserved columns
        │   ├── file_meta.py    # pc_id, file_date from filename, division lookup
        │   ├── periods.py      # Which months a file covers
        │   ├── schema_compare.py # Four July cases (append, reorder, evolve, new table)
        │   └── planner.py      # One action per output
        └── ahi_silver/         # Bronze → Silver rules (pure, unit-tested)
            ├── matching.py     # Voting matcher (saved, exact, fuzzy, semantic, AI)
            ├── llm.py          # AI prompt (few-shot, structured output)
            ├── cleanse.py      # Text, dates, decimals, integers, booleans, derived
            ├── profit_center.py # LOTL lookup, padding, duplicates, test offices
            ├── hashes.py       # Content hashing
            └── loader.py       # Transactional load, mapping save, aggregate rebuild
```

---

## Quick Start (5 Minutes)

### Prerequisites
- Python 3.11+
- Node.js 18+ (for frontend)
- PostgreSQL (for Bronze/Silver ingestion — optional for cleaner-only work)

### 1. Clone & Install Python Dependencies
```bash
git clone <repo-url>
cd AHI_POC
python -m venv .venv
.venv\Scripts\activate  # Windows PowerShell
pip install -r requirements.txt
```

### 2. Run the Cleaner (CLI)
```bash
# Place test files in backend/sample_files_uncleaned/ (git-ignored)
python clean.py
# Outputs: backend/cleaned/, backend/audit/
```

### 3. Run the Web App (Full Stack)

**Terminal 1 — API (from project root):**
```bash
cp .env.example backend/.env
# Edit backend/.env with your Postgres credentials (required for Ingest/Silver steps)
.venv\Scripts\python -m uvicorn api.main:app --app-dir backend --reload --port 8080
```

**Terminal 2 — Frontend:**
```bash
npm --prefix frontend install
npm --prefix frontend run dev
# Open http://localhost:5173
```

### 4. Create First Admin User
```bash
.venv\Scripts\python backend\tools\create_user.py --name "Your Name" --email you@example.com --admin
```

### 5. Run Tests
```bash
python -m pytest                    # All tests (from project root)
python -m pytest backend/tests -k bronze   # Bronze tests only
python -m pytest backend/tests -k silver   # Silver tests only
```

---

## Core Concepts Every Developer Must Know

### 1. The Cleaning Pipeline (ahi_clean)
The pipeline is a **pure function chain** — no database, no external calls. Data flows:
```
Workbook → reader/delimited → pivot detection → orientation → geometry (regions)
  → header detection → row classification → type coercion → orchestrate → CSV + metadata + audit
```

**Key principle**: Every decision derives from data's own shape, types, distinctness, density, arithmetic. **No field names, no schema, no aliases.**

### 2. Job IDs & Output Files
Every cleaning run gets a UUID4 `job_id`. Outputs:
- `<source>_<job_id>.csv` — cleaned table
- `<source>_metadata_<job_id>.csv` — per-column stats
- `<source>.audit.json` — every structural decision with score & reason

### 3. Bronze Layer (File → Bronze)
Loads cleaned tables into Postgres `bronze` schema. Human approves every plan.
- **Source system** from filename suffix (`ARR_pc0515.xlsx` → `pc0515`)
- **pc_id** from filename (`PC796_...` → `PC0796`)
- **file_date** from filename (`2026-06` → 2026-06-01)
- **division_name** from division_mapping by profit center
- **Actions**: CREATE, APPEND, REORDER, EVOLVE, NEW_TABLE, REPLACE, SKIP

### 4. Silver Layer (Bronze → Silver)
Maps bronze columns to unified `silver_detail` (69 cols) + `silver_aggregate` (75 cols).
- **5 voting methods**: Saved mapping, Exact, Fuzzy (rapidfuzz), Semantic (word2vec), AI (Azure OpenAI/Gemini)
- **Reviewer picks** from candidates; approval saves mapping for next time
- **Cleansing**: text trim, date parse (multi-format), decimal parse ($, parentheses), derived fields
- **Profit center** from LOTL table with 4-digit padding

### 5. The UI Workflow (5 Steps)
1. **Configure** — DB settings, reference data, AI keys
2. **Run** — Upload workbook → pick sheets → clean → review results
3. **Results** — Preview, rename headers, export CSV/metadata/audit/zip
4. **Ingest** — Build plan → edit pc_id/date/division → approve → atomic load to bronze
5. **Silver** — Select loads → suggest mapping → review/edit → approve → load to silver + rebuild aggregate

---

## Development Workflow

### Running Tests
```bash
# All tests (pytest.ini points to backend/tests)
python -m pytest

# Specific suites
python -m pytest backend/tests/test_scenarios.py -v      # Scenario corpus tests
python -m pytest backend/tests/test_resilience.py -v     # Resilience & contracts
python -m pytest backend/tests/test_bronze_*.py -v       # Bronze tests
python -m pytest backend/tests/test_silver_*.py -v       # Silver tests

# With coverage
python -m pytest --cov=backend/src --cov=backend/api
```

### Code Quality
```bash
# Type checking (Python)
# No mypy configured yet — PRs welcome to add it

# Type checking (Frontend)
npm --prefix frontend run typecheck

# Linting (Frontend)
# ESLint not configured — uses TypeScript strict mode
```

### Adding a New Cleaning Signal
1. Add scoring primitive to `backend/src/ahi_clean/signals.py`
2. Use it in `header.py`, `geometry.py`, `rowclass.py`, `pivot.py`, or `coerce.py`
3. Add ablation case in `backend/tools/ablation.py`
4. Run `python backend/tools/ablation.py` to verify load-bearing status
5. Add unit tests in `backend/tests/` (build sheets in memory — never name corpus files)

### Adding a New API Endpoint
1. Define Pydantic schemas in `backend/api/schemas.py`
2. Add route in appropriate `backend/api/routes/*.py`
3. Register router in `backend/api/main.py` (with `signed_in` dependency if needed)
4. Add integration test in `backend/tests/test_api.py`

---

## Environment Variables (Copy `.env.example` → `backend/.env`)

| Variable | Required For | Description |
|---|---|---|
| `AHI_DB_HOST/PORT/NAME/USER/PASSWORD` | Bronze/Silver | Postgres for bronze layer |
| `AHI_DB_SSLMODE` | Bronze/Silver | `require` for cloud |
| `AHI_BRONZE_SCHEMA` | Bronze | Default: `bronze` |
| `AHI_CONTROL_SCHEMA` | Bronze | Default: `ingest` |
| `AHI_SILVER_SCHEMA` | Silver | Default: `silver` |
| `AHI_CLEANSED_SCHEMA` | Silver | Default: `bronze_cleansed` |
| `AHI_WORD2VEC_PATH` | Silver semantic | Path to word2vec vectors (Google News 300) |
| `AZURE_OPENAI_*` | Silver AI | Azure OpenAI credentials |
| `GEMINI_API_KEY` | Silver AI | Alternative to Azure |
| `APP_DB_*` | Auth/History | Separate Postgres for users & job history |
| `APP_SESSION_SECRET` | Auth | HMAC key for session cookies (generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`) |
| `APP_COOKIE_SECURE` | Auth | `true` in production (HTTPS) |

---

## Common Tasks

### Score the Cleaner Against Corpus
```bash
python backend/tools/scorecard.py
```

### Benchmark Performance
```bash
python backend/tools/benchmark.py
```

### Ablation Study (What Signals Are Load-Bearing?)
```bash
python backend/tools/ablation.py
```

### Seed Reference Data (Division Mapping, LOTL, DRT)
```bash
python backend/tools/seed_reference.py
# Force reload:
python backend/tools/seed_reference.py --replace
```

### Add Test LOTL Offices
```bash
python backend/tools/seed_reference.py --add-lotl enhancements/test-files/lotl_seed.csv
```

### Generate Silver Test Files
```bash
python backend/tools/make_silver_test_files.py
```

---

## Debugging Tips

### Cleaner Issues
- **Check audit JSON** — every decision with score & reason
- **Check metadata CSV** — per-column stats & `type_flag` (CHECK/INFO)
- **Run single file with verbose**: `python clean.py "path/to/file.xlsx" --out cleaned --audit audit`

### API Issues
- **Health check**: `GET /api/health` — shows config status, max upload, workers
- **Job status**: `GET /api/jobs/{id}` — live stage, current sheet, rows kept/removed
- **Logs**: API logs to stdout (structured JSON in production)

### Database Issues
- **Bronze status**: `GET /api/bronze/status` — configured & reachable?
- **Migrations run automatically** on first use from `backend/migrations/` and `backend/migrations_app/`
- **Connection errors** return 503 with advice (not 500)

### Frontend Issues
- **Vite proxy**: `/api` → `http://localhost:8080` (configurable via `VITE_API_TARGET`)
- **Build for production**: `npm --prefix frontend run build` — uvicorn serves `frontend/dist`

---

## Key Files to Read First

1. **README.md** — Run commands, project overview
2. **BUSINESS_GUIDE.md** — How the cleaner decides (plain language)
3. **ARCHITECTURE.md** — Technical design, diagrams, ablation results
4. **backend/src/ahi_clean/cli.py** — Entry point, parallel workers, error boundaries
5. **backend/api/main.py** — API structure, auth, routers
6. **frontend/src/pages/** — UI pages (Configure, Run, Results, Ingest, Silver)

---

## Testing Philosophy

### Two Test Suites (Deliberately Separated)
| Suite | Scope | Stability |
|---|---|---|
| `test_scenarios.py` | Every workbook in `backend/sample_files_uncleaned/` | Parametrized over whatever is present; new files picked up automatically |
| Everything else | Header, geometry, rowclass, types, appends, pivots, orchestration | Sheets built **in memory**; never names a corpus file |

**Why**: Corpus swaps broke behaviour tests before. In-memory tests survive corpus changes.

### The Oracle
- **Policy numbers (`POL-nnnnnn`)** — every genuine record has one; banners/totals/footers don't
- **Pivot conservation** — unfold must produce exactly one row per value cell
- **Uncheckable files** — reported as unchecked, never scored as pass

### Contracts That Refuse
`backend/src/ahi_clean/contracts.py` checks:
- Row conservation (every row kept or dropped with reason)
- Key column not empty (catches misalignment)
- Empty column explained (catches structural errors)
- Removed total reconciles (kept rows agree with discarded total)

---

## Known Limits (From README)

- **Semantic mapping across sources is out of scope** — `Producer` vs `Agent Name` needs human/LLM/config
- **Leading zero destroyed in source** — unrecoverable (Excel saves `08085` as `8085`)
- **Near-square tables** — 4×5 lookup reads plausibly either way; flagged
- **Gutter ≥2 columns** — read as table boundary unless both sides span same rows
- **Pivot needs ≥3 value columns** — 2-month matrix indistinguishable from 2 numeric cols
- **Output filenames** — carry source stem + job_id, not sheet name (sheet in metadata/audit)
- **Decimal mark** — decided per column from values; ambiguous → US + CHECK flag

---

## Getting Help

- **Architecture questions** → ARCHITECTURE.md (search for section numbers)
- **Business logic questions** → BUSINESS_GUIDE.md
- **API reference** → README.md endpoint tables (Cleaning, Bronze, Silver)
- **Test failures** → Run `python backend/tools/ablation.py` to see if signal is load-bearing
- **Word2vec vectors** → Download `word2vec-google-news-300.gz` from gensim-data releases, place in `backend/models/`, set `AHI_WORD2VEC_PATH`

---

## Useful Commands Cheat Sheet

```bash
# Cleaner
python clean.py                              # Clean all files in sample_files_uncleaned/
python clean.py "inbox/*.xlsx" --workers 8   # Parallel batch
python backend/tools/scorecard.py            # Score against corpus
python backend/tools/benchmark.py            # Throughput measurement
python backend/tools/ablation.py             # Load-bearing signals

# API + Frontend
.venv\Scripts\python -m uvicorn api.main:app --app-dir backend --reload --port 8080
npm --prefix frontend run dev                # Frontend at localhost:5173

# Database
python backend/tools/create_user.py --name "X" --email x@y.com --admin
python backend/tools/seed_reference.py       # Load assets/ into DB
python backend/tools/seed_reference.py --replace  # Force reload

# Tests
python -m pytest                             # All tests
python -m pytest backend/tests -k "bronze"   # Bronze tests
python -m pytest backend/tests -k "silver"   # Silver tests
python -m pytest backend/tests/test_resilience.py -v  # Contracts & resilience
```

---

## First Week Checklist

- [ ] Clone repo, create venv, install deps
- [ ] Run `python clean.py` on sample files, examine `cleaned/`, `audit/`
- [ ] Read BUSINESS_GUIDE.md (30 min)
- [ ] Read ARCHITECTURE.md §1-4, §11 (1 hour)
- [ ] Start API + frontend, create admin user, walk through all 5 UI steps
- [ ] Run full test suite, understand the two test categories
- [ ] Run `scorecard.py`, `benchmark.py`, `ablation.py`
- [ ] Pick a module in `ahi_clean/` and trace a single file through it
- [ ] Make a small change (e.g., add a flag, tweak a threshold), run tests

---

## Architecture Decision Records (Implicit)

| Decision | Rationale |
|---|---|
| No schema/aliases | Real files never agree on names; pipeline must work on unseen columns |
| Polars over pandas | 22x speedup (34 min → 93s for 1M rows); vectorised type decisions |
| Threads over processes | Windows process startup (re-import polars) dwarfs report-sized jobs |
| Exact-header append only | 90% threshold = guessing; better two files than one wrong merge |
| Contracts that refuse | Dangerous failure = plausible but wrong CSV that loads silently |
| Human-in-the-loop for Bronze/Silver | Schema evolution, column mapping, replacements need business judgement |
| In-memory unit tests | Corpus swaps broke file-dependent tests; in-memory survives |
| Ablation as measurement | No assumption quietly becomes load-bearing without showing up |

---

## Contacts & Escalation

- **Cleaning pipeline logic** → Check `ahi_clean/` module docs + ARCHITECTURE.md
- **Bronze/Silver business rules** → `enhancements/FILE_TO_BRONZE.md` and `enhancements/BRONZE_TO_SILVER.md`
- **UI/UX** → `frontend/src/pages/`, `design-system/MASTER.md`
- **Infrastructure/Deployment** → Not yet documented (POC stage)

---

Welcome to the team! 🎯 This is a unique codebase — the cleaning pipeline is essentially a **compiler for spreadsheets**. Treat it like one: every pass has a clear input/output, every decision is recorded, and the tests are your regression suite.