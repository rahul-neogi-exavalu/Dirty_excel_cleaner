"""In-memory registry of uploads and jobs.

A single-process service, so a dict behind a lock is the whole persistence story. The
files themselves live under ``WORK_DIR``; losing this registry on restart means the UI
asks the user to upload again, which the not-found error already says.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from .errors import not_found

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

# The pipeline's stages in order, as the UI's stepper shows them.
STAGES = ["read", "clean", "append", "validate", "write"]


@dataclass
class Upload:
    id: str
    filename: str
    path: Path
    size: int
    kind: str  # "workbook" | "delimited"
    sheets: list[dict]
    uploaded_at: float = field(default_factory=time.time)


@dataclass
class OutputRecord:
    """One cleaned table the job produced, plus the user's header edits."""

    id: str
    name: str
    kind: str  # "stacked" | "standalone"
    frame: pl.DataFrame
    tables: list[str]
    sheet_names: list[str]
    type_flags: dict[str, str]
    inferred: dict[str, str]
    file: str
    metadata_file: str
    # original column name -> the name the user gave it. Only changed names are kept.
    renames: dict[str, str] = field(default_factory=dict)
    headers_updated_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def current_name(self, original: str) -> str:
        return self.renames.get(original, original)

    @property
    def columns(self) -> list[str]:
        return [self.current_name(name) for name in self.frame.columns]


@dataclass
class Job:
    id: str
    workbook_id: str
    source_name: str
    sheets: list[str]
    append: bool
    status: str = QUEUED
    stage: str | None = None
    progress: float = 0.0
    message: str = "Queued"
    current_sheet: str | None = None
    sheets_done: int = 0
    rows_kept: int = 0
    rows_removed: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: dict | None = None
    cancel_requested: bool = False
    outputs: list[OutputRecord] = field(default_factory=list)
    sheet_reports: list[dict] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    append_check: dict | None = None
    # Consistency report per output id (services/consistency_service.py).
    consistency: dict[str, dict] = field(default_factory=dict)
    violations: list[dict] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    audit_path: Path | None = None
    directory: Path | None = None

    def output(self, output_id: str) -> OutputRecord:
        for record in self.outputs:
            if record.id == output_id:
                return record
        raise not_found("That cleaned table")


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self._uploads: dict[str, Upload] = {}
        self._jobs: dict[str, Job] = {}

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def add_upload(self, upload: Upload) -> None:
        with self._lock:
            self._uploads[upload.id] = upload

    def upload(self, upload_id: str) -> Upload:
        with self._lock:
            found = self._uploads.get(upload_id)
        if found is None:
            raise not_found("That workbook")
        return found

    def remove_upload(self, upload_id: str) -> Upload:
        with self._lock:
            found = self._uploads.pop(upload_id, None)
        if found is None:
            raise not_found("That workbook")
        return found

    def add_job(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def job(self, job_id: str) -> Job:
        with self._lock:
            found = self._jobs.get(job_id)
        if found is None:
            raise not_found("That cleaning job")
        return found

    def jobs_for(self, upload_id: str) -> list[Job]:
        with self._lock:
            return [job for job in self._jobs.values() if job.workbook_id == upload_id]


store = Store()
