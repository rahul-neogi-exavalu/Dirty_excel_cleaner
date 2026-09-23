#!/usr/bin/env python
"""Write per-file metadata CSVs for the cleaner's generated CSV outputs.
This file creates transposed metadata summaries for the cleaner's output CSV files."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

METADATA_SUFFIX = "_metadata.csv"
FIELDNAMES = [
    "header_name",
    "distinct_count",
    "count",
    "total_row_count",
    "sheet_info",
]


def generate_metadata(out_dir="cleaned", audit_dir="audit") -> list[Path]:
    """Create one transposed metadata CSV for every cleaned CSV in ``out_dir``."""
    output_dir = Path(out_dir)
    audit_sources = _audit_sources(Path(audit_dir))
    written = []

    for csv_path in sorted(output_dir.glob("*.csv")):
        if csv_path.name.endswith(METADATA_SUFFIX):
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
                "source_sheets": output.get("source_sheets", []),
                "columns": output.get("columns", []),
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
        for sheet_info, sheet_rows in groups:
            total_rows = len(sheet_rows)
            for header in headers:
                values = [row.get(header, "") for row in sheet_rows]
                populated = [value for value in values if value != ""]
                writer.writerow(
                    {
                        "sheet_info": sheet_info,
                        "header_name": header,
                        "distinct_count": len(set(populated)),
                        "count": len(populated),
                        "total_row_count": total_rows,
                    }
                )


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
