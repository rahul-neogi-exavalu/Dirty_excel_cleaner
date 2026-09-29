"""Read-side of a finished job: summaries, a paginated preview, the column profile, and
header renames.

The cleaned frame is never modified. A rename is stored as a mapping from the column's
original name to the user's, and applied wherever the column is shown or exported, so
the cleaner's own decisions -- types, flags, per-sheet grouping -- stay keyed on the
names it gave them.
"""

from __future__ import annotations

import datetime as _dt
import math
import time
from decimal import Decimal

import polars as pl

from ahi_clean import metadata

from .. import config
from ..errors import ApiError, conflict
from ..store import SUCCEEDED, Job, OutputRecord, store


def finished_job(job_id: str) -> Job:
    job = store.job(job_id)
    if job.status != SUCCEEDED:
        raise conflict(
            "Results are not available for this job yet.",
            "Run the cleaning job to completion, then review its results.",
        )
    return job


def output_summary(record: OutputRecord) -> dict:
    return {
        "id": record.id,
        "name": record.name,
        "kind": record.kind,
        "rows": len(record.frame),
        "columns": len(record.frame.columns),
        "column_names": record.columns,
        "tables": record.tables,
        "sheet_names": record.sheet_names,
        "flagged_columns": sum(1 for flag in record.type_flags.values() if "CHECK" in flag),
        "renamed_columns": len(record.renames),
        "headers_updated_at": record.headers_updated_at,
        "file": record.file,
        "metadata_file": record.metadata_file,
    }


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #


def preview(record: OutputRecord, offset: int, limit: int, query: str | None,
            sort: str | None, descending: bool) -> dict:
    frame = record.frame
    total_unfiltered = len(frame)

    if query and query.strip():
        needle = query.strip().casefold()
        frame = frame.filter(
            pl.any_horizontal(
                [
                    pl.col(name).cast(pl.String).str.to_lowercase().str.contains(needle, literal=True)
                    for name in frame.columns
                ]
            ).fill_null(False)
        )

    if sort:
        original = _original_name(record, sort)
        frame = frame.sort(original, descending=descending, nulls_last=True)

    window = frame.slice(offset, limit)
    return {
        "columns": [
            {
                "name": record.current_name(name),
                "original": name,
                "dtype": str(record.frame.schema[name]),
                "renamed": name in record.renames,
            }
            for name in record.frame.columns
        ],
        "rows": [[_json_value(value) for value in row] for row in window.iter_rows()],
        "offset": offset,
        "limit": limit,
        "total": len(frame),
        "total_unfiltered": total_unfiltered,
    }


def _json_value(value):
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (_dt.date, _dt.datetime, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, str, bool)):
        return value
    return str(value)


def _original_name(record: OutputRecord, name: str) -> str:
    """Resolve a column by its original name first, then by the user's current name.

    The UI always addresses columns by original name, which never changes; accepting
    the current name as well keeps the API usable by hand.
    """
    if name in record.frame.columns:
        return name
    for original in record.frame.columns:
        if record.current_name(original) == name:
            return original
    raise ApiError(422, "unknown_column", f"There is no column named '{name}'.", field="sort")


# --------------------------------------------------------------------------- #
# Column profile (the metadata file, under the current header names)
# --------------------------------------------------------------------------- #


def column_profile(job: Job, record: OutputRecord) -> list[dict]:
    return build_metadata(job, record).to_dicts()


def build_metadata(job: Job, record: OutputRecord) -> pl.DataFrame:
    """The cleaner's metadata, with header_name showing the user's names.

    Built from the original frame so an appended table is still described sheet by
    sheet (``metadata.build`` groups on ``source_sheet`` by name) and every flag and
    inferred type still finds its column. Only the displayed name changes.
    """
    described = metadata.build(
        record.frame, job.source_name, record.sheet_names, record.inferred, record.type_flags
    )
    if not record.renames:
        return described
    return described.with_columns(
        pl.col("header_name").replace(record.renames).alias("header_name")
    )


# --------------------------------------------------------------------------- #
# Header renames
# --------------------------------------------------------------------------- #


def validate_header(name) -> str:
    if not isinstance(name, str):
        raise ApiError(422, "invalid_header", "Column name must be text.")
    cleaned = name.strip()
    if not cleaned:
        raise ApiError(422, "empty_header", "Column name cannot be empty.")
    if len(cleaned) > config.MAX_HEADER_LENGTH:
        raise ApiError(
            422, "header_too_long",
            f"Column name cannot be longer than {config.MAX_HEADER_LENGTH} characters.",
        )
    if any(ord(ch) < 32 for ch in cleaned):
        raise ApiError(422, "invalid_header", "Column name cannot contain line breaks or control characters.")
    return cleaned


def rename_headers(record: OutputRecord, renames: dict[str, str]) -> OutputRecord:
    """Apply renames atomically: all of them pass validation, or none is stored."""
    with record.lock:
        mapping = dict(record.renames)
        for key, new in renames.items():
            original = _original_name(record, key)
            cleaned = validate_header(new)
            if cleaned == original:
                mapping.pop(original, None)
            else:
                mapping[original] = cleaned

        final = [mapping.get(name, name) for name in record.frame.columns]
        seen: dict[str, str] = {}
        for original, name in zip(record.frame.columns, final):
            folded = name.casefold()
            if folded in seen:
                raise ApiError(
                    422,
                    "duplicate_header",
                    f"Column name '{name}' already exists.",
                    "Each column in a table needs a unique name.",
                    field=original,
                )
            seen[folded] = original

        record.renames = mapping
        record.headers_updated_at = time.time()
    return record


def reset_headers(record: OutputRecord, columns: list[str] | None) -> OutputRecord:
    with record.lock:
        if columns is None:
            record.renames = {}
        else:
            for name in columns:
                record.renames.pop(_original_name(record, name), None)
        record.headers_updated_at = time.time()
    return record

