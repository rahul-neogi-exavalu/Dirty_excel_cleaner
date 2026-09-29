from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ..services import export_service, results_service

router = APIRouter(prefix="/api/jobs/{job_id}", tags=["exports"])


def _download(path, media_type: str) -> FileResponse:
    return FileResponse(path, media_type=media_type, filename=path.name,
                        headers={"Cache-Control": "no-store"})


@router.get("/outputs/{output_id}/export/csv")
def export_csv(job_id: str, output_id: str) -> FileResponse:
    job = results_service.finished_job(job_id)
    return _download(export_service.csv_file(job, job.output(output_id)), "text/csv")


@router.get("/outputs/{output_id}/export/metadata")
def export_metadata(job_id: str, output_id: str) -> FileResponse:
    job = results_service.finished_job(job_id)
    return _download(export_service.metadata_file(job, job.output(output_id)), "text/csv")


@router.get("/export/audit")
def export_audit(job_id: str) -> FileResponse:
    job = results_service.finished_job(job_id)
    return _download(export_service.audit_file(job), "application/json")


@router.get("/export/zip")
def export_zip(job_id: str) -> FileResponse:
    job = results_service.finished_job(job_id)
    return _download(export_service.bundle(job), "application/zip")
