#!/usr/bin/env python
"""Write per-file metadata CSVs for the cleaner's generated CSV outputs.
This file creates transposed metadata summaries for the cleaner's output CSV files."""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

METADATA_SUFFIX = "_metadata.csv"
FIELDNAMES = [
    "header_name",
    "distinct_count",
    "count",
    "total_row_count",
    "min",
    "max",
    "sum",
    "null_percentage",
    "sheet_info",
]


def generate_metadata(out_dir="cleaned", audit_dir="audit") -> list[Path]:
    """Create one transposed metadata CSV for every cleaned CSV in ``out_dir``."""
    output_dir = Path(out_dir)
    audit_sources = _audit_sources(Path(audit_dir))
    written = []

    for csv_path in sorted(output_dir.glob("*.csv")):
        if _is_metadata_path(csv_path) or _has_job_id(csv_path):
            continue
        metadata_path = csv_path.with_name(f"{csv_path.stem}{METADATA_SUFFIX}")
        _write_metadata(csv_path, metadata_path, audit_sources.get(csv_path.name, {}))
        written.append(metadata_path)

    return written


def generate_metadata_from_args(argv=None) -> list[Path]:
    """Resolve the cleaner's output options and generate the companion files."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--out", default="cleaned")
    parser.add_argument("--audit", default="audit")
    args, _unknown = parser.parse_known_args(argv)
    return generate_metadata(args.out, args.audit)


def rename_exports_with_job_ids(
    out_dir="cleaned", audit_dir="audit", since_ns=0, argv=None
) -> list[Path]:
    """Rename outputs written since ``since_ns`` and their metadata companions."""
    if argv is not None:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--out", default=out_dir)
        parser.add_argument("--audit", default=audit_dir)
        args, _unknown = parser.parse_known_args(argv)
        out_dir, audit_dir = args.out, args.audit

    output_dir = Path(out_dir)
    audit_sources = _audit_sources(Path(audit_dir))
    renamed = []

    for csv_path in sorted(output_dir.glob("*.csv")):
        if _is_metadata_path(csv_path) or _has_job_id(csv_path):
            continue
        try:
            if csv_path.stat().st_mtime_ns < since_ns:
                continue
        except OSError:
            continue

        audit_info = audit_sources.get(csv_path.name, {})
        source_file = audit_info.get("source_file")
        source_stem = Path(source_file).stem if source_file else csv_path.stem
        job_id = str(uuid.uuid4())
        new_csv = output_dir / f"{source_stem}_{job_id}.csv"
        metadata_path = csv_path.with_name(f"{csv_path.stem}{METADATA_SUFFIX}")
        new_metadata = output_dir / f"{source_stem}_metadata_{job_id}.csv"
        csv_path.rename(new_csv)
        if metadata_path.exists():
            metadata_path.rename(new_metadata)
            _set_cleaned_file_name(new_metadata, new_csv.name)
        renamed.append(new_csv)

    return renamed


def _is_metadata_path(path: Path) -> bool:
    return path.name.endswith(METADATA_SUFFIX) or "_metadata_" in path.stem


_JOB_ID_SUFFIX = re.compile(
    r"_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _has_job_id(path: Path) -> bool:
    return bool(_JOB_ID_SUFFIX.search(path.stem))


def _audit_sources(audit_dir: Path) -> dict[str, dict]:
    sources: dict[str, dict] = {}
    if not audit_dir.exists():
        return sources

    for report_path in audit_dir.glob("*.audit.json"):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for output in report.get("outputs", []):
            sources[output.get("file", "")] = {
                "source_file": report.get("source_file", ""),
                "source_sheets": output.get("source_sheets", []),
                "columns": output.get("columns", []),
                "types_by_sheet": {
                    trace.get("sheet", ""): trace.get("inferred_types", {})
                    for trace in report.get("tables", [])
                    if trace.get("sheet")
                },
            }
    return sources


def _write_metadata(csv_path: Path, metadata_path: Path, audit_info: dict) -> None:
    with csv_path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        headers = reader.fieldnames or audit_info.get("columns", [])
        rows = list(reader)

    groups = _group_rows(rows, headers, audit_info.get("source_sheets", []))
    with metadata_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=FIELDNAMES)
        writer.writeheader()
        for group_sheet_info, sheet_rows in groups:
            total_rows = len(sheet_rows)
            for header in headers:
                values = [row.get(header, "") for row in sheet_rows]
                populated = [value for value in values if value != ""]
                stats = _column_statistics(
                    values,
                    header,
                    audit_info.get("types_by_sheet", {}),
                    group_sheet_info,
                )
                writer.writerow(
                    {
                        "sheet_info": csv_path.name,
                        "header_name": header,
                        "distinct_count": len(set(populated)),
                        "count": len(populated),
                        "total_row_count": total_rows,
                        **stats,
                    }
                )


def _set_cleaned_file_name(metadata_path: Path, cleaned_name: str) -> None:
    with metadata_path.open("r", newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    with metadata_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            row["sheet_info"] = cleaned_name
            writer.writerow(row)


def _column_statistics(values, header, types_by_sheet, sheet_info) -> dict[str, str]:
    total = len(values)
    populated = [value.strip() for value in values if value is not None and value.strip()]
    null_percentage = (total - len(populated)) / total * 100 if total else 0.0
    kind = _column_kind(header, types_by_sheet, sheet_info, populated)

    result = {
        "min": "",
        "max": "",
        "sum": "",
        "null_percentage": f"{null_percentage:.2f}",
    }
    if kind == "numeric":
        numbers = [_to_decimal(value) for value in populated]
        numbers = [number for number in numbers if number is not None]
        if numbers:
            result["min"] = _decimal_text(min(numbers))
            result["max"] = _decimal_text(max(numbers))
            result["sum"] = _decimal_text(sum(numbers, Decimal("0")))
    elif kind == "date":
        dates = [_to_date(value) for value in populated]
        dates = [value for value in dates if value is not None]
        if dates:
            result["min"] = min(dates).isoformat()
            result["max"] = max(dates).isoformat()
    return result


def _column_kind(header, types_by_sheet, sheet_info, populated) -> str:
    sheets = [part.strip() for part in sheet_info.split(";") if part.strip()]
    kinds = {
        types_by_sheet.get(sheet, {}).get(header)
        for sheet in sheets
        if types_by_sheet.get(sheet, {}).get(header)
    }
    if kinds & {"int", "float"}:
        return "numeric"
    if kinds & {"date", "datetime", "date_string"}:
        return "date"
    if populated and all(_to_date(value) is not None for value in populated):
        return "date"
    if populated and all(_to_decimal(value) is not None for value in populated):
        return "numeric"
    return "other"


def _to_decimal(value):
    try:
        return Decimal(value.replace(",", "").replace("%", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def _to_date(value):
    try:
        return _dt.date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _group_rows(rows: list[dict], headers: list[str], audit_sheets: list[str]):
    if "source_sheet" in headers:
        grouped = defaultdict(list)
        for row in rows:
            grouped[row.get("source_sheet", "") or "unknown"].append(row)
        return sorted(grouped.items())

    sheet_info = "; ".join(str(sheet) for sheet in audit_sheets if sheet) or "unknown"
    return [(sheet_info, rows)]


if __name__ == "__main__":
    generate_metadata_from_args()
