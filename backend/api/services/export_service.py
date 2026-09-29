"""Produce the deliverables on request, always from the latest saved headers.

Exports are regenerated each time rather than served from the files written at the end
of the run, so a header rename saved a minute ago is in the file downloaded now. The
file names keep the CLI's pattern -- ``{stem}_{job_id}.csv`` and
``{stem}_metadata_{job_id}.csv`` -- so a downstream loader cannot tell which produced it.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from .. import config
from ..errors import ApiError, conflict
from ..store import SUCCEEDED, Batch, Job, OutputRecord
from . import results_service


def _export_dir(job: Job) -> Path:
    folder = job.directory / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def csv_file(job: Job, record: OutputRecord) -> Path:
    with record.lock:
        frame = record.frame.rename(record.renames) if record.renames else record.frame
        path = _export_dir(job) / record.file
        frame.write_csv(path)
    return path


def metadata_file(job: Job, record: OutputRecord) -> Path:
    with record.lock:
        described = results_service.build_metadata(job, record)
        path = _export_dir(job) / record.metadata_file
        described.write_csv(path)
    return path


def audit_file(job: Job) -> Path:
    if job.audit_path is None or not job.audit_path.exists():
        raise ApiError(404, "not_found", "The audit report for this job is missing.",
                       "Run the cleaning job again to regenerate it.")
    return job.audit_path


def bundle(job: Job) -> Path:
    """Every CSV and metadata file, plus the audit report, in one zip."""
    stem = Path(job.source_name).stem
    path = _export_dir(job) / f"{stem}_cleaned_{job.id}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _add_job(archive, job, "")
    return path


def batch_bundle(batch: Batch, jobs: list[Job]) -> Path:
    """Every successfully cleaned file of a batch, one folder per source file.

    Files that failed or were cancelled have nothing to export and are left out; two
    uploads with the same name get distinct folders so neither overwrites the other.
    """
    done = [job for job in jobs if job.status == SUCCEEDED]
    if not done:
        raise conflict(
            "No file in this batch was cleaned successfully, so there is nothing to export.",
            "Fix the files that failed and run the batch again.",
        )
    folder = config.JOB_DIR / f"batch-{batch.id}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"cleaned_batch_{batch.id}.zip"
    used: set[str] = set()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for job in done:
            name = base = Path(job.source_name).stem or "file"
            counter = 2
            while name.casefold() in used:
                name = f"{base} ({counter})"
                counter += 1
            used.add(name.casefold())
            _add_job(archive, job, f"{name}/")
    return path


def _add_job(archive: zipfile.ZipFile, job: Job, prefix: str) -> None:
    for record in job.outputs:
        archive.write(csv_file(job, record), prefix + record.file)
        archive.write(metadata_file(job, record), prefix + record.metadata_file)
    archive.write(audit_file(job), prefix + job.audit_path.name)
