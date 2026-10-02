"""Service settings, overridable from the environment."""

from __future__ import annotations

import os
from pathlib import Path

# backend/ -- the API's own files live under here; the UI is a sibling folder.
ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ROOT.parent

try:
    from dotenv import load_dotenv
except ImportError:  # optional: plain environment variables work without it
    pass
else:
    # A real environment variable always wins over a file; backend/.env over the
    # project root's .env (first loaded wins, since nothing is overridden).
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(PROJECT_ROOT / ".env", override=False)

# Uploads, job outputs and exports live here. Git-ignored; safe to delete between runs.
WORK_DIR = Path(os.environ.get("AHI_WORK_DIR", ROOT / ".workspace"))
UPLOAD_DIR = WORK_DIR / "uploads"
JOB_DIR = WORK_DIR / "jobs"

MAX_UPLOAD_MB = int(os.environ.get("AHI_MAX_UPLOAD_MB", "500"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
# Files cleaned together in one batch. Each is its own job.
MAX_BATCH_FILES = int(os.environ.get("AHI_MAX_BATCH_FILES", "20"))


def _default_workers() -> int:
    """Worker processes for reading and cleaning sheets side by side.

    The work is CPU-bound Python (XML parsing, row classification, typing). Under the GIL
    threads would take turns on one core, so the parallelism comes from processes, and a
    CPU-bound process pool is sized by cores -- one per core. The "2 x cores" rule of
    thumb is for threads that mostly *wait* (network, disk); here a second worker per core
    would only time-slice the same core while holding a second sheet in memory.

    Measured on a 4-core / 8-thread laptop, 8 sheets of 150k cells: 4 workers 13.9 s,
    8 workers 11.1 s -- physical cores give most of the gain, hyper-threads ~20% more.

    One logical core is left for the API process itself: it streams uploads, answers
    progress polls, and does each file's final steps (append, checks, writing CSVs).
    Memory is the other bound: a worker idles at ~60 MB and needs ~0.35 KB per cell of the
    sheet it holds, so at least 1 GB of RAM is kept per worker, and 8 is the ceiling.
    """
    cpus = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    by_memory = _total_memory_bytes() // (1024**3) or 8
    return max(1, min(cpus - 1, by_memory, 8))


def _total_memory_bytes() -> int:
    """Physical memory, best effort without extra dependencies; 0 when unknown."""
    try:
        if os.name == "nt":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                            ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                            ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                            ("extended", ctypes.c_ulonglong)]

            status = _Status()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.total)
            return 0
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return 0


CLEAN_WORKERS = max(1, int(os.environ.get("AHI_CLEAN_WORKERS") or _default_workers()))
# Below this file size a single process finishes before worker hand-off would pay off.
PARALLEL_MIN_BYTES = int(os.environ.get("AHI_PARALLEL_MIN_BYTES", str(256 * 1024)))
# Files of one batch in progress at once. Their sheets share the one worker pool, so
# this does not add CPU pressure; it keeps the pool busy while a file is being opened
# or written, and lets a batch of single-sheet files use more than one core.
BATCH_FILE_CONCURRENCY = max(1, int(os.environ.get("AHI_BATCH_FILE_CONCURRENCY", "2")))

# What the reader can open. Workbooks go through openpyxl, the rest through the
# delimited reader, which treats the file as a single sheet.
WORKBOOK_SUFFIXES = {".xlsx", ".xlsm"}
DELIMITED_SUFFIXES = {".csv", ".tsv", ".txt"}
ALLOWED_SUFFIXES = WORKBOOK_SUFFIXES | DELIMITED_SUFFIXES

MAX_HEADER_LENGTH = 128
PREVIEW_MAX_LIMIT = 100

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
CORS_ORIGINS = os.environ.get(
    "AHI_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")

# --- Bronze layer (Postgres) -------------------------------------------------
# Cleaned tables are ingested into Postgres. Placeholders live in .env.example; the
# Ingest step stays locked until host, database and user are all set.
DB_HOST = os.environ.get("AHI_DB_HOST", "").strip()
DB_PORT = int(os.environ.get("AHI_DB_PORT") or "5432")
DB_NAME = os.environ.get("AHI_DB_NAME", "").strip()
DB_USER = os.environ.get("AHI_DB_USER", "").strip()
DB_PASSWORD = os.environ.get("AHI_DB_PASSWORD", "")
DB_SSLMODE = os.environ.get("AHI_DB_SSLMODE", "prefer").strip()
# Raw cleaned tables, and the registry / audit tables that describe them.
BRONZE_SCHEMA = os.environ.get("AHI_BRONZE_SCHEMA", "bronze").strip()
CONTROL_SCHEMA = os.environ.get("AHI_CONTROL_SCHEMA", "ingest").strip()
# The team adds the source system to the file name as a suffix, e.g. ARR_pc0515.xlsx.
# One capture group; the last match in the file stem wins.
SOURCE_SYSTEM_PATTERN = os.environ.get(
    "AHI_SOURCE_SYSTEM_PATTERN", r"(?:^|[_\-\s.])([a-z]{1,6}[_\-]?\d{2,6})(?=$|[_\-\s.])"
)
# Unfilled .env.example placeholders (<your-postgres-host>) count as not configured.
DB_CONFIGURED = all(value and not value.startswith("<") for value in (DB_HOST, DB_NAME, DB_USER))
INGEST_ENABLED = os.environ.get("AHI_INGEST_ENABLED", "true").lower() not in ("0", "false", "no")


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "")


# --- Silver layer --------------------------------------------------------------
SILVER_SCHEMA = os.environ.get("AHI_SILVER_SCHEMA", "silver").strip()
# The AHI doc's LOTL (pc_id -> legacy_office_name -> profit center number). Until the
# real one exists this is a placeholder in the control schema, seeded for testing.
LOTL_TABLE = os.environ.get("AHI_LOTL_TABLE", "").strip() or f"{CONTROL_SCHEMA}.lotl"
# The Silver target columns (DRT). A draft ships; the business replaces it.
SILVER_COLUMNS_FILE = Path(os.environ.get("AHI_SILVER_COLUMNS_FILE", ROOT / "config" / "silver_columns.csv"))
# Thresholds for a fuzzy or word2vec vote.
MATCH_FUZZY_MIN = float(os.environ.get("AHI_MATCH_FUZZY_MIN") or "85")
MATCH_SEMANTIC_MIN = float(os.environ.get("AHI_MATCH_SEMANTIC_MIN") or "0.72")
# word2vec vectors (text or binary word2vec format). Unset: no word2vec vote.
WORD2VEC_PATH = os.environ.get("AHI_WORD2VEC_PATH", "").strip()
if WORD2VEC_PATH.startswith("<"):  # the .env.example placeholder
    WORD2VEC_PATH = ""
elif WORD2VEC_PATH and not Path(WORD2VEC_PATH).is_absolute():
    # Relative paths are relative to the project root, wherever the API is started from.
    WORD2VEC_PATH = str(PROJECT_ROOT / WORD2VEC_PATH)


def _setting(name: str, default: str = "") -> str:
    """A value, or "" for an unfilled .env.example placeholder (<your-...>)."""
    value = os.environ.get(name, default).strip()
    return "" if value.startswith("<") or "<your" in value else value


# The AI vote: Azure OpenAI (default) or Gemini. Samples stay local unless allowed.
AZURE_OPENAI_API_KEY = _setting("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = _setting("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = _setting("AZURE_OPENAI_API_VERSION") or "2024-10-21"
AZURE_OPENAI_DEPLOYMENT = _setting("AZURE_OPENAI_DEPLOYMENT_NAME")
GEMINI_API_KEY = _setting("GEMINI_API_KEY")
GEMINI_MODEL = _setting("AHI_GEMINI_MODEL") or "gemini-2.5-flash"

_AZURE_READY = bool(AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT)
_GEMINI_READY = bool(GEMINI_API_KEY) and _flag("AHI_GEMINI_ENABLED", "true")
AI_PROVIDER = (_setting("AHI_AI_PROVIDER").lower()
               or ("azure_openai" if _AZURE_READY else "gemini" if _GEMINI_READY else ""))
AI_ENABLED = _flag("AHI_AI_ENABLED", "true") and (
    (AI_PROVIDER == "azure_openai" and _AZURE_READY) or (AI_PROVIDER == "gemini" and _GEMINI_READY))
# What the UI shows: provider and model, never a key.
AI_LABEL = (f"Azure OpenAI · {AZURE_OPENAI_DEPLOYMENT}" if AI_PROVIDER == "azure_openai"
            else f"Gemini · {GEMINI_MODEL}" if AI_PROVIDER == "gemini" else "")
# The older AHI_GEMINI_SEND_SAMPLES name is still honoured.
AI_SEND_SAMPLES = _flag("AHI_AI_SEND_SAMPLES", os.environ.get("AHI_GEMINI_SEND_SAMPLES", "false"))


def db_conninfo() -> str:
    """libpq connection string for the bronze database."""
    from psycopg.conninfo import make_conninfo

    return make_conninfo(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER,
        password=DB_PASSWORD or None, sslmode=DB_SSLMODE or None,
        application_name="exavalu-cleaning-studio",
    )


# --- App database: users and job history ----------------------------------------
# A database of its own (it may share the server with bronze). Sign-in needs it; the
# cleaning service records every job here when it is configured.
APP_DB_HOST = _setting("APP_DB_HOST")
APP_DB_PORT = int(os.environ.get("APP_DB_PORT") or "5432")
APP_DB_NAME = _setting("APP_DB_NAME")
APP_DB_USER = _setting("APP_DB_USER")
APP_DB_PASSWORD = os.environ.get("APP_DB_PASSWORD", "")
APP_DB_SSLMODE = os.environ.get("APP_DB_SSLMODE", "prefer").strip()
APP_DB_SCHEMA = _setting("APP_DB_SCHEMA") or "public"
APP_DB_CONFIGURED = all((APP_DB_HOST, APP_DB_NAME, APP_DB_USER))

# Sessions are an HMAC-SHA256-signed cookie. Without a fixed secret one is made per
# process, so every restart signs everyone out.
APP_SESSION_SECRET = _setting("APP_SESSION_SECRET")
APP_SESSION_HOURS = float(os.environ.get("APP_SESSION_HOURS") or "12")
# true behind HTTPS; plain http://localhost needs false or the browser drops the cookie.
APP_COOKIE_SECURE = _flag("APP_COOKIE_SECURE", "false")


def app_db_conninfo() -> str:
    """libpq connection string for the app database."""
    from psycopg.conninfo import make_conninfo

    return make_conninfo(
        host=APP_DB_HOST, port=APP_DB_PORT, dbname=APP_DB_NAME, user=APP_DB_USER,
        password=APP_DB_PASSWORD or None, sslmode=APP_DB_SSLMODE or None,
        application_name="exavalu-cleaning-studio-app",
    )
