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


class BatchCreate(BaseModel):
    """Several uploaded files cleaned in one go: each with its own sheets and append mode."""

    files: list[JobCreate] = Field(min_length=1)

    @field_validator("files")
    @classmethod
    def _one_entry_per_file(cls, files: list[JobCreate]) -> list[JobCreate]:
        ids = [item.workbook_id for item in files]
        if len(set(ids)) != len(ids):
            raise ValueError("each file can only be included once")
        return files


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
    batch_id: str | None = None
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
    active_sheets: list[str] = []
    parallel_workers: int = 0
    sheets_done: int
    sheets_total: int
    rows_kept: int
    rows_removed: int
    created_at: float
    started_at: float | None
    finished_at: float | None
    elapsed_seconds: float | None
    error: JobError | None = None


class BatchStatus(BaseModel):
    id: str
    # queued | running | succeeded | partial | failed | cancelled
    status: str
    progress: float
    files_total: int
    files_done: int
    files_succeeded: int
    files_failed: int
    files_cancelled: int
    current_job_id: str | None
    jobs: list[JobStatus]
    created_at: float
    started_at: float | None
    finished_at: float | None
    elapsed_seconds: float | None


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
    # Columns the user left out of bronze ingestion.
    excluded_columns: int = 0
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
    # Left out of bronze ingestion by the user.
    excluded: bool = False


class Preview(BaseModel):
    columns: list[PreviewColumn]
    rows: list[list[Any]]
    offset: int
    limit: int
    total: int
    total_unfiltered: int


class SourcePreview(BaseModel):
    """A window of one uploaded sheet's cells, as the file holds them (before cleaning)."""

    sheet: str
    hidden: bool
    source_format: str
    # The used extent: the sheet row of the last value, and the last used column.
    total_rows: int
    total_columns: int
    # Rows that can be paged through; fewer than total_rows for a sheet too large to hold.
    available_rows: int
    offset: int
    limit: int
    col_offset: int
    col_limit: int
    # Column letters of the window; row numbers are offset + 1 onwards.
    columns: list[str]
    rows: list[list[Any]]
    # Merged ranges (A1:B2) that touch the window, and how many the sheet has in all.
    merged: list[str]
    merged_total: int


class HeaderUpdate(BaseModel):
    # current (or original) column name -> new name
    renames: dict[str, str] = Field(min_length=1)


class HeaderReset(BaseModel):
    columns: list[str] | None = None


class ColumnExclusion(BaseModel):
    # current (or original) column names
    columns: list[str] = Field(min_length=1)
    # True leaves them out of bronze ingestion; False brings them back.
    excluded: bool


# --- Bronze ingestion ----------------------------------------------------------


class PlanCreate(BaseModel):
    # Control rows to load; none: every row staged and not loaded yet.
    control_ids: list[int] | None = None
    batch_id: str | None = None


class PlanItemUpdate(BaseModel):
    table_name: str | None = None
    action: str | None = None


class PlanApprove(BaseModel):
    # Ignored: the signed-in user is the reviewer. Kept so older clients still validate.
    reviewed_by: str | None = None
    confirmed: list[str] = []


class PlanFile(BaseModel):
    """A staged control row in the plan: decided in Validate, read-only here."""

    key: str
    control_id: int
    file_name: str
    output_name: str | None = None
    source_system: str
    pc_id: str | None
    division_name: str | None
    # ISO dates (YYYY-MM-DD).
    file_received_date: str | None
    reporting_start_date: str
    reporting_end_date: str
    reporting_period_type: str | None
    date_detail: str | None
    processing_action: str
    file_replaced: str | None
    replace_month: str | None
    rows: int
    fitness: int | None = None
    staging_table: str
    staged_at: float | None = None


class PlanOut(BaseModel):
    id: str
    batch_id: str | None
    status: str
    progress: float
    message: str
    error: dict[str, Any] | None
    reviewed_by: str | None
    files: list[PlanFile]
    items: list[dict[str, Any]]
    # What still stands between the plan and approval (blockers only; confirmations
    # are the reviewer's to give).
    blockers: list[str]
    results: dict[str, Any]
    created_at: float
    started_at: float | None
    finished_at: float | None


# --- Validate (before Bronze) --------------------------------------------------------


class ValidationCreate(BaseModel):
    job_ids: list[str] = Field(min_length=1)
    batch_id: str | None = None


class ValidationFileUpdate(BaseModel):
    # PC0796 (796 and PC796 are accepted), YYYY-MM-DD, a division name.
    pc_id: str | None = None
    file_received_date: str | None = None
    division_name: str | None = None


class ValidationOutputUpdate(BaseModel):
    # Required column (Silver name) -> the output's column (original name), or null: missing.
    mapping: dict[str, str | None] | None = None
    # YYYY-MM (whole months); both null: back to what the data says.
    reporting_start_date: str | None = None
    reporting_end_date: str | None = None
    # reject | replace (an older year-to-date file) | replace_month (a month already loaded) | null
    choice: str | None = None
    # Use the dates the control table lists for this file.
    use_control_dates: bool | None = None


class ValidationStage(BaseModel):
    # Output keys to stage; none: every output not staged yet.
    keys: list[str] | None = None
