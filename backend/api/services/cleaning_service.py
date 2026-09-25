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

from ahi_clean import audit, contracts, failures, metadata, orchestrate
from ahi_clean.extract import extract_sheet
from ahi_clean.reader import count_embedded_images, read_workbook

from .. import config
from ..errors import ApiError, conflict
from ..schemas import JobCreate
from . import append_report, consistency_service
from ..store import (
    CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED, Job, OutputRecord, store,
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
    "read": "Reading workbook",
    "clean": "Cleaning sheets",
    "append": "Matching cleaned headers",
    "validate": "Checking output contracts",
    "write": "Writing cleaned files",
}


class _Cancelled(Exception):
    pass


class _NoTable(Exception):
    pass


def start_job(request: JobCreate) -> Job:
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
    active = [job for job in store.jobs_for(upload.id) if job.status in (QUEUED, RUNNING)]
    if active:
        raise conflict(
            "A cleaning job for this workbook is already running.",
            "Wait for it to finish or cancel it before starting another.",
        )

    # Keep the workbook's own sheet order regardless of the order they were ticked.
    ordered = [name for name in known if name in set(request.sheets)]
    job = Job(
        id=store.new_id(),
        workbook_id=upload.id,
        source_name=upload.filename,
        sheets=ordered,
        append=request.append,
    )
    job.directory = config.JOB_DIR / job.id
    store.add_job(job)
    threading.Thread(target=_run, args=(job,), name=f"clean-{job.id}", daemon=True).start()
    return job


def cancel_job(job_id: str) -> Job:
    job = store.job(job_id)
    if job.status not in (QUEUED, RUNNING):
        raise conflict("This job has already finished and can't be cancelled.")
    job.cancel_requested = True
    job.message = "Cancelling after the current sheet"
    return job


# --------------------------------------------------------------------------- #


def _enter(job: Job, stage: str, fraction: float = 0.0, message: str | None = None) -> None:
    if job.cancel_requested:
        raise _Cancelled()
    start, end = _SPAN[stage]
    job.stage = stage
    job.progress = round(start + (end - start) * fraction, 4)
    job.message = message or _STAGE_MESSAGES[stage]


def _run(job: Job) -> None:
    upload = store.upload(job.workbook_id)
    source = upload.path
    stem = source.stem
    job.status = RUNNING
    job.started_at = time.time()
    try:
        _enter(job, "read", message=f"Reading {upload.filename}")
        wanted = set(job.sheets)
        grids = [grid for grid in read_workbook(source) if grid.name in wanted]

        # One pass: every selected sheet is read once and cleaned once, here.
        results = []
        for index, grid in enumerate(grids):
            job.current_sheet = grid.name
            _enter(job, "clean", index / max(len(grids), 1), f"Cleaning {grid.name}")
            sheet_results = extract_sheet(grid)
            results.extend(sheet_results)
            job.sheets_done = index + 1
            job.rows_kept += sum(len(result.frame) for result in sheet_results)
            job.rows_removed += sum(len(result.trace.get("dropped_rows", [])) for result in sheet_results)
        job.current_sheet = None

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
    except _Cancelled:
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
        job.finished_at = time.time()


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
