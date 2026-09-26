"""Run the cleaner as a background job the UI can watch.

The steps mirror ``cli.clean_workbook`` one for one -- read, extract, plan, check
contracts, write CSV + metadata + audit -- and call the same functions. They are laid
out here rather than called through ``clean_workbook`` for two reasons the CLI does not
have: progress has to be reported sheet by sheet while it runs, and the user chooses
which sheets to clean and whether matching ones are appended.

Each selected sheet is read and cleaned exactly once, inside the job. Nothing about the
tables is known before that. Header detection, junk-row removal
and typing all happen in ``extract_sheet``; whether two sheets can be appended depends
on the *cleaned* headers, so that decision is made -- and reported -- afterwards.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from ahi_clean import audit, contracts, failures, metadata, orchestrate
from ahi_clean.reader import count_embedded_images

from .. import config
from ..errors import ApiError, conflict
from ..schemas import BatchCreate, JobCreate
from . import append_report, consistency_service, sheet_pool
from ..store import (
    ACTIVE, CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED, Batch, Job, OutputRecord, store,
)

# Share of the progress bar each stage occupies; cleaning sheets is the long part.
_SPAN = {
    "read": (0.0, 0.15),
    "clean": (0.15, 0.80),
    "append": (0.80, 0.86),
    "validate": (0.86, 0.90),
    "write": (0.90, 1.0),
}

_STAGE_MESSAGES = {
    "read": "Opening workbook",
    "clean": "Reading and cleaning sheets",
    "append": "Matching cleaned headers",
    "validate": "Checking output contracts",
    "write": "Writing cleaned files",
}


# Guards a queued job's first transition -- to running or to cancelled -- so a cancel
# arriving just as the batch picks the job up cannot be lost.
_transition = threading.Lock()


class _Cancelled(Exception):
    pass


class _NoTable(Exception):
    pass


def _prepare(request: JobCreate, batch_id: str | None = None) -> Job:
    """Validate one file's request and build its job, without starting it."""
    upload = store.upload(request.workbook_id)
    known = [sheet["name"] for sheet in upload.sheets]
    unknown = [name for name in request.sheets if name not in known]
    if unknown:
        raise ApiError(
            422,
            "unknown_sheet",
            f"{len(unknown)} selected sheet(s) are not in {upload.filename}: {', '.join(unknown)}.",
            "Refresh the sheet list and select again.",
            field="sheets",
        )
    active = [job for job in store.jobs_for(upload.id) if job.status in ACTIVE]
    if active:
        raise conflict(
            f"A cleaning job for {upload.filename} is already running.",
            "Wait for it to finish or cancel it before starting another.",
        )

    # Keep the workbook's own sheet order regardless of the order they were ticked.
    ordered = [name for name in known if name in set(request.sheets)]
    job = Job(
        id=store.new_id(),
        workbook_id=upload.id,
        source_name=upload.filename,
        sheets=ordered,
        # Appending needs two tables to compare; with one sheet there is nothing to do.
        append=request.append and len(ordered) > 1,
        batch_id=batch_id,
    )
    job.directory = config.JOB_DIR / job.id
    return job


def start_job(request: JobCreate) -> Job:
    job = _prepare(request)
    store.add_job(job)
    threading.Thread(target=_run, args=(job,), name=f"clean-{job.id}", daemon=True).start()
    return job


def cancel_job(job_id: str) -> Job:
    job = store.job(job_id)
    if job.status not in ACTIVE:
        raise conflict("This job has already finished and can't be cancelled.")
    with _transition:
        if job.status == QUEUED and job.batch_id:
            # Still waiting its turn in a batch: it never starts.
            _mark_cancelled(job, "Cancelled before it started")
            return job
    job.cancel_requested = True
    job.message = "Cancelling after the current sheet"
    return job


# --------------------------------------------------------------------------- #
# Batches: several files, one job each, run one after another
# --------------------------------------------------------------------------- #


def start_batch(request: BatchCreate) -> Batch:
    if len(request.files) > config.MAX_BATCH_FILES:
        raise ApiError(
            422,
            "too_many_files",
            f"A batch can hold up to {config.MAX_BATCH_FILES} files; {len(request.files)} were sent.",
            "Remove some files, or clean them in more than one batch.",
            field="files",
        )
    batch_id = store.new_id()
    # Every file is validated before any job exists, so a bad selection in the last
    # file never leaves the first ones running on their own.
    jobs = [_prepare(item, batch_id) for item in request.files]
    batch = Batch(id=batch_id, job_ids=[job.id for job in jobs])
    store.add_batch(batch, jobs)
    threading.Thread(target=_run_batch, args=(batch, jobs), name=f"batch-{batch.id}", daemon=True).start()
    return batch


def cancel_batch(batch_id: str) -> Batch:
    batch = store.batch(batch_id)
    jobs = store.batch_jobs(batch)
    if not any(job.status in ACTIVE for job in jobs):
        raise conflict("This batch has already finished and can't be cancelled.")
    with _transition:
        batch.cancel_requested = True
        for job in jobs:
            if job.status == QUEUED:
                _mark_cancelled(job, "Cancelled before it started")
            elif job.status == RUNNING:
                job.cancel_requested = True
                job.message = "Cancelling after the current sheet"
    return batch


def batch_state(batch: Batch, jobs: list[Job]) -> str:
    if any(job.status in ACTIVE for job in jobs) or batch.finished_at is None:
        return RUNNING if batch.started_at else QUEUED
    succeeded = sum(1 for job in jobs if job.status == SUCCEEDED)
    if batch.cancel_requested:
        return CANCELLED
    if succeeded == len(jobs):
        return SUCCEEDED
    if succeeded == 0:
        return FAILED if any(job.status == FAILED for job in jobs) else CANCELLED
    return "partial"


def _mark_cancelled(job: Job, message: str) -> None:
    job.status = CANCELLED
    job.message = message
    job.finished_at = time.time()


def _run_batch(batch: Batch, jobs: list[Job]) -> None:
    """Run a batch's files, up to ``BATCH_FILE_CONCURRENCY`` at a time, in submitted order.

    The files' sheets all go to the same worker pool, so two files in flight do not mean
    twice the CPU: they keep the pool fed while one file is opened or written, and let a
    batch of single-sheet files use more than one core.
    """
    batch.started_at = time.time()
    try:
        with ThreadPoolExecutor(
            max_workers=min(config.BATCH_FILE_CONCURRENCY, len(jobs)) or 1,
            thread_name_prefix=f"batch-{batch.id}",
        ) as files:
            # Submitted in order and started in order, so the first file finishes first
            # when files are alike -- and the list on screen fills from the top.
            for job in jobs:
                files.submit(_run_batch_file, batch, job)
    finally:
        batch.finished_at = time.time()


def _run_batch_file(batch: Batch, job: Job) -> None:
    with _transition:
        if job.status != QUEUED:
            return  # cancelled while it waited
        if batch.cancel_requested:
            _mark_cancelled(job, "Cancelled before it started")
            return
        job.status = RUNNING
    try:
        _run(job)
    except Exception as error:  # noqa: BLE001 - one file must not stop the rest
        job.status = FAILED
        job.error = {
            "kind": "internal",
            "message": f"{job.source_name} could not be processed.",
            "detail": str(error),
            "advice": "Upload the file again and retry.",
            "stage": job.stage,
            "sheet": job.current_sheet,
            "technical": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        }
        job.message = "Cleaning could not be completed"
        job.finished_at = time.time()


# --------------------------------------------------------------------------- #


def _enter(job: Job, stage: str, fraction: float = 0.0, message: str | None = None) -> None:
    if job.cancel_requested:
        raise _Cancelled()
    start, end = _SPAN[stage]
    job.stage = stage
    job.progress = round(start + (end - start) * fraction, 4)
    job.message = message or _STAGE_MESSAGES[stage]


def _run(job: Job) -> None:
    try:
        upload = store.upload(job.workbook_id)
    except ApiError:
        job.status = FAILED
        job.error = {
            "kind": "not_found",
            "message": f"{job.source_name} is no longer available.",
            "detail": "The uploaded file was removed before it could be cleaned.",
            "advice": "Upload the file again and rerun.",
            "stage": None, "sheet": None, "technical": None,
        }
        job.message = "Cleaning could not be completed"
        job.finished_at = time.time()
        return
    source = upload.path
    stem = source.stem
    job.status = RUNNING
    job.started_at = time.time()
    try:
        _enter(job, "read", message=f"Opening {upload.filename}")
        grids, results = _clean_sheets(job, source)

        live = [result for result in results if not result.frame.is_empty()]
        if not live:
            raise _NoTable()

        _enter(job, "append")
        # Read the headers before planning, which renames adopted headerless tables.
        job.append_check = append_report.build(results) if job.append else None
        outputs, workbook_report = _plan(results, stem, job.append)

        _enter(job, "validate")
        violations = contracts.check_all(results)
        workbook_report["contract_violations"] = [item.as_dict() for item in violations]

        _enter(job, "write")
        records = _write(job, source, stem, outputs)

        report = audit.build_report(
            source,
            [result.trace for result in results],
            workbook_report,
            outputs,
            count_embedded_images(source),
        )
        job.audit_path = audit.write_report(report, job.directory, stem)

        job.outputs = records
        job.violations = workbook_report["contract_violations"]
        job.consistency = consistency_service.build(grids, results, outputs, job.violations)
        job.relationships = _relationships(results, workbook_report, job.append)
        job.sheet_reports = _sheet_reports(grids, results, outputs)
        job.summary = _summary(job, grids, results, outputs, report)
        job.progress = 1.0
        job.stage = "write"
        job.message = "Cleaning completed"
        job.status = SUCCEEDED
    except (_Cancelled, sheet_pool.Cancelled):
        job.status = CANCELLED
        job.message = "Job cancelled"
    except _NoTable:
        job.status = FAILED
        job.error = {
            "kind": failures.NO_TABLE,
            "message": "No table-shaped data was found in the selected sheets.",
            "detail": "Every selected sheet was read, but none held a header row with data beneath it.",
            "advice": failures.Failure(source, failures.NO_TABLE, "").advice
            + ". Select sheets that contain tabular data.",
            "stage": job.stage,
            "sheet": None,
            "technical": None,
        }
        job.message = "Cleaning could not be completed"
    except Exception as error:  # noqa: BLE001 - the job must always end in a state
        if isinstance(error, sheet_pool.SheetFailed):
            # Parallel sheets finish in any order: name the one that actually failed.
            job.current_sheet = error.sheet
            error = error.__cause__ or error
        failure = failures.classify(source, error)
        where = f"the {job.current_sheet} sheet" if job.current_sheet else upload.filename
        job.status = FAILED
        job.error = {
            "kind": failure.kind,
            "message": f"Something went wrong while processing {where}.",
            "detail": failure.detail,
            "advice": failure.advice,
            "stage": job.stage,
            "sheet": job.current_sheet,
            "technical": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        }
        job.message = "Cleaning could not be completed"
    finally:
        job.active_sheets = []
        job.finished_at = time.time()


def _clean_sheets(job: Job, source):
    """Read and clean every selected sheet -- each exactly once -- in workbook order.

    Sheets are independent until the append step, so they are spread over the worker
    pool; a small file stays in this process, where there is no hand-off to pay for.
    """
    parallel = sheet_pool.use_parallel(source)
    job.parallel_workers = min(config.CLEAN_WORKERS, len(job.sheets)) if parallel else 0
    total = max(len(job.sheets), 1)
    _enter(job, "clean", 0.0)

    def on_start(active: list[str]) -> None:
        job.active_sheets = active
        job.current_sheet = active[0] if active else None
        if len(active) > 1:
            job.message = f"Cleaning {len(active)} sheets in parallel · {job.sheets_done} of {total} done"
        elif active:
            job.message = f"Cleaning {active[0]}"

    def on_done(name: str, grid, sheet_results) -> None:
        job.sheets_done += 1
        job.rows_kept += sum(len(result.frame) for result in sheet_results)
        job.rows_removed += sum(len(result.trace.get("dropped_rows", [])) for result in sheet_results)
        start, end = _SPAN["clean"]
        job.progress = round(start + (end - start) * job.sheets_done / total, 4)

    pairs = sheet_pool.run_sheets(
        source,
        job.sheets,
        parallel=parallel,
        cancelled=lambda: job.cancel_requested,
        on_start=on_start,
        on_done=on_done,
    )
    job.current_sheet = None
    job.active_sheets = []
    grids = [grid for grid, _ in pairs]
    return grids, [result for _, sheet_results in pairs for result in sheet_results]


def _plan(results, stem: str, append: bool):
    """Append identical-header tables, or ship every table on its own.

    With append on this is exactly the CLI's ``plan_workbook``. With it off, each table
    is planned alone, so the exact-header grouping never has two tables to stack; sibling
    header adoption still runs first, because naming a headerless continuation sheet is
    part of cleaning it, not part of appending it.
    """
    if append:
        return orchestrate.plan_workbook(results, stem)

    adoptions = orchestrate.adopt_sibling_headers(results)
    live = [result for result in results if not result.frame.is_empty()]
    outputs, roles = [], []
    for result in live:
        planned, report = orchestrate.plan_workbook([result], stem)
        roles.extend(report["table_roles"])
        for output in planned:
            if len(live) > 1:
                output.name = f"{stem}_{orchestrate._slug(result.label)}"
            outputs.append(output)
    return outputs, {"table_roles": roles, "relationships": [], "header_adoptions": adoptions}


def _write(job: Job, source, stem: str, outputs) -> list[OutputRecord]:
    job.directory.mkdir(parents=True, exist_ok=True)
    records = []
    for index, output in enumerate(outputs):
        _enter(job, "write", index / max(len(outputs), 1), f"Writing {output.name}")
        # Same naming as the CLI: one id names the CSV and its metadata.
        output.job_id = str(uuid.uuid4())
        output.file = f"{stem}_{output.job_id}.csv"
        output.metadata_file = f"{stem}_metadata_{output.job_id}.csv"

        path = job.directory / output.file
        output.frame.write_csv(path)
        inferred = metadata.inferred_schema(path)
        described = metadata.build(output.frame, source.name, output.sheet_names, inferred, output.type_flags)
        described.write_csv(job.directory / output.metadata_file)

        records.append(
            OutputRecord(
                id=output.job_id,
                name=output.name,
                kind=output.kind,
                frame=output.frame,
                tables=list(output.sheets),
                sheet_names=list(output.sheet_names),
                type_flags=dict(output.type_flags),
                inferred=inferred,
                file=output.file,
                metadata_file=output.metadata_file,
            )
        )
    return records


def _relationships(results, workbook_report, append: bool) -> list[dict]:
    """What happened to each group of tables with identical headers."""
    groups: dict[frozenset, list[str]] = {}
    for result in results:
        if result.frame.is_empty():
            continue
        key = orchestrate.header_key(result)
        if key is not None:
            groups.setdefault(key, []).append(result.label)

    relationships = [
        {"tables": item["tables"], "decision": "appended", "reason": "identical cleaned headers"}
        for item in workbook_report.get("relationships", [])
    ]
    if not append:
        relationships.extend(
            {
                "tables": labels,
                "decision": "kept_separate",
                "reason": "identical cleaned headers, but append was turned off",
            }
            for labels in groups.values()
            if len(labels) > 1
        )
    return relationships


def _sheet_reports(grids, results, outputs) -> list[dict]:
    """Per selected sheet: its raw extent, and each table the cleaner found in it."""
    destination = {label: output.job_id for output in outputs for label in output.sheets}
    reports = []
    for grid in grids:
        tables = []
        for result in results:
            if result.sheet_name != grid.name:
                continue
            trace = result.trace
            header = trace.get("header") or {}
            dropped = trace.get("dropped_rows", [])
            tables.append(
                {
                    "label": result.label,
                    "rows": len(result.frame),
                    "columns": len(result.frame.columns),
                    "region_rows": (trace.get("region_shape") or [None, None])[0],
                    "region_columns": (trace.get("region_shape") or [None, None])[1],
                    "header_detected": bool(header.get("detected")),
                    "header_row": _header_row(trace),
                    "header_adopted_from": header.get("adopted_from"),
                    "orientation": (trace.get("orientation") or {}).get("orientation", "upright"),
                    "removed_rows": len(dropped),
                    "removed_by_reason": dict(Counter(row.get("classification", "OTHER") for row in dropped)),
                    "removed_examples": [
                        {k: row.get(k) for k in ("sheet_row", "classification", "reason", "content")}
                        for row in dropped[:25]
                    ],
                    "coercion_failures": len(trace.get("coercion_failures", [])),
                    "empty_columns": list(trace.get("empty_columns", [])),
                    "notes": list(trace.get("notes", [])),
                    "output_id": destination.get(result.label),
                }
            )
        live = [table for table in tables if table["rows"] or table["columns"]]
        reports.append(
            {
                "sheet": grid.name,
                "raw_rows": grid.height,
                "raw_columns": grid.width,
                "tables": live,
                "rows": sum(table["rows"] for table in live),
                "removed_rows": sum(table["removed_rows"] for table in tables),
                "status": "cleaned" if live else "no_table",
            }
        )
    return reports


def _header_row(trace) -> int | None:
    """1-based sheet row the header was found on, when the cleaner recorded one."""
    header = trace.get("header") or {}
    index = header.get("row_index")
    origin = (trace.get("region_origin") or {}).get("row")
    if index is None or origin is None:
        return None
    return origin + index + 1


def _summary(job: Job, grids, results, outputs, report) -> dict:
    audit_summary = report.get("summary", {})
    return {
        "source_name": job.source_name,
        "append": job.append,
        "sheets_selected": len(job.sheets),
        "sheets_with_tables": len({r.sheet_name for r in results if not r.frame.is_empty()}),
        "tables_found": sum(1 for r in results if not r.frame.is_empty()),
        "outputs": len(outputs),
        "appended_outputs": sum(1 for output in outputs if output.kind == "stacked"),
        "rows": sum(len(output.frame) for output in outputs),
        "raw_rows": sum(grid.height for grid in grids),
        "rows_removed": audit_summary.get("rows_dropped", 0),
        "removed_by_reason": audit_summary.get("dropped_by_classification", {}),
        "coercion_failures": audit_summary.get("coercion_failures", 0),
        "validation_findings": audit_summary.get("validation_findings", 0),
        "contract_violations": audit_summary.get("contract_violations", 0),
        # Failed consistency checks across all tables (contracts + row accounting).
        "consistency_issues": sum(report["issues"] for report in job.consistency.values()),
        "headerless_tables": audit_summary.get("headerless_tables", 0),
        "flagged_columns": sum(
            sum(1 for flag in output.type_flags.values() if "CHECK" in flag) for output in outputs
        ),
        "embedded_images": report.get("embedded_images", 0),
        "completed_at": time.time(),
    }
