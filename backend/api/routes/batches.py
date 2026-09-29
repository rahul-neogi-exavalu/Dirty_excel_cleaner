from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ..schemas import BatchCreate, BatchStatus
from ..services import cleaning_service, export_service
from ..store import ACTIVE, CANCELLED, FAILED, SUCCEEDED, Batch, store
from .jobs import status_of

router = APIRouter(prefix="/api/batches", tags=["batches"])


def batch_status(batch: Batch) -> BatchStatus:
    jobs = store.batch_jobs(batch)
    finished = [job for job in jobs if job.status not in ACTIVE]
    current = next((job for job in jobs if job.status == "running"), None)
    progress = sum(1.0 if job.status not in ACTIVE else job.progress for job in jobs) / max(len(jobs), 1)
    elapsed = None
    if batch.started_at:
        elapsed = round((batch.finished_at or time.time()) - batch.started_at, 2)
    return BatchStatus(
        id=batch.id,
        status=cleaning_service.batch_state(batch, jobs),
        progress=round(progress, 4),
        files_total=len(jobs),
        files_done=len(finished),
        files_succeeded=sum(1 for job in jobs if job.status == SUCCEEDED),
        files_failed=sum(1 for job in jobs if job.status == FAILED),
        files_cancelled=sum(1 for job in jobs if job.status == CANCELLED),
        current_job_id=current.id if current else None,
        jobs=[status_of(job) for job in jobs],
        created_at=batch.created_at,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        elapsed_seconds=elapsed,
    )


@router.post("", response_model=BatchStatus, status_code=202)
def create_batch(request: BatchCreate) -> BatchStatus:
    return batch_status(cleaning_service.start_batch(request))


@router.get("/{batch_id}", response_model=BatchStatus)
def get_batch(batch_id: str) -> BatchStatus:
    return batch_status(store.batch(batch_id))


@router.post("/{batch_id}/cancel", response_model=BatchStatus)
def cancel_batch(batch_id: str) -> BatchStatus:
    return batch_status(cleaning_service.cancel_batch(batch_id))


@router.get("/{batch_id}/export/zip")
def export_batch_zip(batch_id: str) -> FileResponse:
    batch = store.batch(batch_id)
    path = export_service.batch_bundle(batch, store.batch_jobs(batch))
    return FileResponse(path, media_type="application/zip", filename=path.name,
                        headers={"Cache-Control": "no-store"})
