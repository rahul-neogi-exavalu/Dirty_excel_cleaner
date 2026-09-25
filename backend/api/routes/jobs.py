from __future__ import annotations

import time

from fastapi import APIRouter, Query

from .. import config
from ..schemas import (
    HeaderReset, HeaderUpdate, JobCreate, JobResults, JobStatus, OutputSummary, Preview,
)
from ..services import cleaning_service, results_service
from ..store import STAGES, Job, store

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def status_of(job: Job) -> JobStatus:
    elapsed = None
    if job.started_at:
        elapsed = round((job.finished_at or time.time()) - job.started_at, 2)
    return JobStatus(
        id=job.id,
        workbook_id=job.workbook_id,
        source_name=job.source_name,
        sheets=job.sheets,
        append=job.append,
        status=job.status,
        stage=job.stage,
        stages=STAGES,
        progress=job.progress,
        message=job.message,
        current_sheet=job.current_sheet,
        sheets_done=job.sheets_done,
        sheets_total=len(job.sheets),
        rows_kept=job.rows_kept,
        rows_removed=job.rows_removed,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        elapsed_seconds=elapsed,
        error=job.error,
    )


@router.post("", response_model=JobStatus, status_code=202)
def create_job(request: JobCreate) -> JobStatus:
    return status_of(cleaning_service.start_job(request))


@router.get("/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    return status_of(store.job(job_id))


@router.post("/{job_id}/cancel", response_model=JobStatus)
def cancel_job(job_id: str) -> JobStatus:
    return status_of(cleaning_service.cancel_job(job_id))


@router.get("/{job_id}/results", response_model=JobResults)
def get_results(job_id: str) -> JobResults:
    job = results_service.finished_job(job_id)
    return JobResults(
        job=status_of(job),
        summary=job.summary,
        outputs=[results_service.output_summary(record) for record in job.outputs],
        sheets=job.sheet_reports,
        relationships=job.relationships,
        violations=job.violations,
        append_check=job.append_check,
        consistency=job.consistency,
    )


@router.get("/{job_id}/outputs/{output_id}/preview", response_model=Preview)
def get_preview(
    job_id: str,
    output_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=config.PREVIEW_MAX_LIMIT),
    q: str | None = Query(None, max_length=200),
    sort: str | None = Query(None, max_length=config.MAX_HEADER_LENGTH),
    desc: bool = False,
) -> Preview:
    job = results_service.finished_job(job_id)
    record = job.output(output_id)
    return Preview(**results_service.preview(record, offset, limit, q, sort, desc))


@router.get("/{job_id}/outputs/{output_id}/columns")
def get_columns(job_id: str, output_id: str) -> list[dict]:
    job = results_service.finished_job(job_id)
    return results_service.column_profile(job, job.output(output_id))


@router.put("/{job_id}/outputs/{output_id}/headers", response_model=OutputSummary)
def update_headers(job_id: str, output_id: str, body: HeaderUpdate) -> OutputSummary:
    job = results_service.finished_job(job_id)
    record = results_service.rename_headers(job.output(output_id), body.renames)
    return OutputSummary(**results_service.output_summary(record))


@router.post("/{job_id}/outputs/{output_id}/headers/reset", response_model=OutputSummary)
def reset_headers(job_id: str, output_id: str, body: HeaderReset) -> OutputSummary:
    job = results_service.finished_job(job_id)
    record = results_service.reset_headers(job.output(output_id), body.columns)
    return OutputSummary(**results_service.output_summary(record))
