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
