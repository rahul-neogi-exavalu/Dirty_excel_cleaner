"""Assemble and write the per-workbook audit report.

The report is the deliverable that makes the cleaning reviewable: every row the
pipeline threw away, every structural guess it made, and every join value that a
human still needs to look at.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path


def build_report(source_path, sheet_traces, workbook_report, outputs, image_count) -> dict:
    """Combine sheet traces and workbook decisions into one report."""
    needs_review = list((workbook_report.get("join") or {}).get("needs_review", []))

    return {
        "source_file": str(source_path),
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "embedded_images": image_count,
        "tables": sheet_traces,
        "workbook": workbook_report,
        "outputs": [
            {
                "file": f"{output.name}.csv",
                "kind": output.kind,
                "source_sheets": output.sheets,
                "rows": int(len(output.frame)),
                "columns": list(output.frame.columns),
            }
            for output in outputs
        ],
        "summary": {
            "rows_dropped": sum(len(trace.get("dropped_rows", [])) for trace in sheet_traces),
            "dropped_by_classification": _count_classifications(sheet_traces),
            "coercion_failures": sum(len(trace.get("coercion_failures", [])) for trace in sheet_traces),
            "validation_findings": sum(len(trace.get("validation", [])) for trace in sheet_traces),
            "join_values_needing_review": len(needs_review),
            "contract_violations": len(workbook_report.get("contract_violations", [])),
            "tables_found": len(sheet_traces),
            "headerless_tables": sum(
                1 for trace in sheet_traces if not trace.get("header", {}).get("detected", True)
            ),
            "low_confidence_orientations": sum(
                1 for trace in sheet_traces if not trace.get("orientation", {}).get("confident", True)
            ),
        },
        "needs_human_review": needs_review,
    }


def _count_classifications(sheet_traces) -> dict:
    counts: dict[str, int] = {}
    for trace in sheet_traces:
        for dropped in trace.get("dropped_rows", []):
            key = dropped["classification"]
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def write_report(report: dict, audit_dir, stem: str) -> Path:
    path = Path(audit_dir) / f"{stem}.audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path
