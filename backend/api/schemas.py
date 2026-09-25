"""Request and response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class SheetInfo(BaseModel):
    name: str
    hidden: bool = False
    has_content: bool = True


class WorkbookOut(BaseModel):
    id: str
    filename: str
    size: int
    kind: str
    sheets: list[SheetInfo]
    uploaded_at: float


class JobCreate(BaseModel):
    workbook_id: str = Field(min_length=1)
    sheets: list[str] = Field(min_length=1)
    append: bool = True

    @field_validator("sheets")
    @classmethod
    def _unique(cls, sheets: list[str]) -> list[str]:
        if len(set(sheets)) != len(sheets):
            raise ValueError("each sheet can only be selected once")
        return sheets


class JobError(BaseModel):
    kind: str
    message: str
    detail: str | None = None
    advice: str | None = None
    stage: str | None = None
    sheet: str | None = None
    technical: str | None = None


class JobStatus(BaseModel):
    id: str
    workbook_id: str
    source_name: str
    sheets: list[str]
    append: bool
    status: str
    stage: str | None
    stages: list[str]
    progress: float
    message: str
    current_sheet: str | None
    sheets_done: int
    sheets_total: int
    rows_kept: int
    rows_removed: int
    created_at: float
    started_at: float | None
    finished_at: float | None
    elapsed_seconds: float | None
    error: JobError | None = None


class OutputSummary(BaseModel):
    id: str
    name: str
    kind: str
    rows: int
    columns: int
    column_names: list[str]
    tables: list[str]
    sheet_names: list[str]
    flagged_columns: int
    renamed_columns: int
    headers_updated_at: float | None
    file: str
    metadata_file: str


class JobResults(BaseModel):
    job: JobStatus
    summary: dict[str, Any]
    outputs: list[OutputSummary]
    sheets: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    violations: list[dict[str, Any]]
    # With auto-detect on: which selected tables were appended and which could not be,
    # with the column differences. None when the user chose to keep sheets separate.
    append_check: dict[str, Any] | None = None
    # Per output id: every consistency check (pass / fail / not applicable) and the
    # sheet-level row accounting.
    consistency: dict[str, Any] = {}


class PreviewColumn(BaseModel):
    name: str
    original: str
    dtype: str
    renamed: bool


class Preview(BaseModel):
    columns: list[PreviewColumn]
    rows: list[list[Any]]
    offset: int
    limit: int
    total: int
    total_unfiltered: int


class HeaderUpdate(BaseModel):
    # current (or original) column name -> new name
    renames: dict[str, str] = Field(min_length=1)


class HeaderReset(BaseModel):
    columns: list[str] | None = None
