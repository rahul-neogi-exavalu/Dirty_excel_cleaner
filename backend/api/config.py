"""Service settings, overridable from the environment."""

from __future__ import annotations

import os
from pathlib import Path

# backend/ -- the API's own files live under here; the UI is a sibling folder.
ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ROOT.parent

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
