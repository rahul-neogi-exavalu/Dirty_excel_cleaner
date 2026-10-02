"""Consistency checks per cleaned table: is it safe to load?

Two sources, both evidence the run already has:

* The cleaner's own output contracts (``ahi_clean.contracts``), which run inside each
  table region: row conservation, totals reconciliation, key and empty columns,
  duplicate names. They are reported here pass / fail / not applicable per table,
  not only when they fail.
* A sheet-level row accounting, added here. The contracts check rows *inside* the
  detected table region; a non-empty row outside every region (a stray title above
  the table, a note below it) is neither kept nor logged as removed and so disappears
  without a trace. This closes that gap::

      raw rows = blank + header + removed (each with a logged reason) + kept

  Anything left over is unaccounted for and is listed row by row.
"""

from __future__ import annotations

from collections import Counter

from ahi_clean import contracts, typing_utils
from ahi_clean.typing_utils import is_blank

CHECKS = [
    (contracts.ROW_CONSERVATION, "No rows lost inside the table",
     "Inside the table, every row must be the header, a logged removal, or kept."),
    (contracts.EMPTY_KEY_COLUMN, "Key columns are filled",
     "An ID column that comes out entirely empty means values shifted away from their header."),
    (contracts.DUPLICATE_COLUMNS, "Column names are unique",
     "Repeated column names make a table impossible to load."),
]

PASSED, FAILED, NOT_APPLICABLE = "passed", "failed", "not_applicable"


def build(grids, results, outputs, violations: list[dict]) -> dict[str, dict]:
    """One report per output, keyed by the output's own id."""
    by_sheet: dict[str, list] = {}
    for result in results:
        by_sheet.setdefault(result.sheet_name, []).append(result)
    accounting = {grid.name: _account(grid, by_sheet.get(grid.name, [])) for grid in grids}
    by_label = {result.label: result for result in results}

    reports = {}
    for output in outputs:
        tables = [by_label[label] for label in output.sheets if label in by_label]
        sheets = [accounting[name] for name in dict.fromkeys(output.sheet_names) if name in accounting]
        failures = [item for item in violations if item["table"] in output.sheets]
        reports[output.output_id] = _report(output.output_id, tables, sheets, failures)
    return reports


def _report(output_id: str, tables: list, sheets: list[dict], failures: list[dict]) -> dict:
    checks = []
    for check_id, title, description in CHECKS:
        found = [item for item in failures if item["check"] == check_id]
        if not found and not _applies(check_id, tables):
            continue  # nothing to check, so the check is not shown
        checks.append({"id": check_id, "title": title, "description": description,
                       "status": FAILED if found else PASSED,
                       "details": [f"{item['table']}: {item['detail']}" for item in found]})

    issues = sum(1 for check in checks if check["status"] == FAILED)
    return {
        "output_id": output_id,
        "status": FAILED if issues else PASSED,
        "issues": issues,
        "checks": checks,
        "accounting": sheets,
    }


def _applies(check_id: str, tables: list) -> bool:
    """Whether a passing check had anything to check, so 'passed' is not claimed vacuously."""
    if check_id == contracts.EMPTY_KEY_COLUMN:
        return any(kind == typing_utils.ID_STRING
                   for table in tables for kind in table.trace.get("inferred_types", {}).values())
    return True


# --------------------------------------------------------------------------- #
# Sheet-level row accounting
# --------------------------------------------------------------------------- #


def _account(grid, results: list) -> dict:
    orientations = [result.trace.get("orientation") or {} for result in results]
    sheet_flipped = any(item.get("decided_by") == "sheet_level_content_scores" for item in orientations)
    region_flipped = not sheet_flipped and any(item.get("orientation") == "transposed" for item in orientations)

    # A sheet turned upright as a whole was cut into tables along its columns, so the
    # records are counted along that axis too.
    lines = [list(column) for column in zip(*grid.rows)] if sheet_flipped else grid.rows
    axis = "columns" if sheet_flipped else "rows"

    # A line holding only Excel errors reads as empty once the errors are nulled, but it
    # held something: it is accounted for as a removal (EXCEL_ERROR), not as blank.
    errors = grid.error_lines(transposed=sheet_flipped) if hasattr(grid, "error_lines") else {}
    blank = {
        index for index, line in enumerate(lines)
        if index not in errors and all(is_blank(cell) for cell in line)
    }
    header = sum(_header_lines(result) for result in results if not result.frame.is_empty())
    dropped = sorted(
        (row for result in results for row in result.trace.get("dropped_rows", [])),
        key=lambda row: row.get("sheet_row") or 0,
    )
    kept = sum(_kept_source_lines(result) for result in results)
    unaccounted = len(lines) - len(blank) - header - len(dropped) - kept

    report = {
        "sheet": grid.name,
        "axis": axis,
        "raw": len(lines),
        "blank": len(blank),
        "header": header,
        "removed": len(dropped),
        "removed_by_reason": dict(Counter(row.get("classification", "OTHER") for row in dropped)),
        "kept": kept,
        "unaccounted": unaccounted,
        "unaccounted_rows": [],
        "removed_rows": [
            {key: row.get(key) for key in ("sheet_row", "classification", "reason", "content")}
            for row in dropped[:50]
        ],
        "status": PASSED if unaccounted == 0 else FAILED,
        "note": None,
    }

    if region_flipped:
        # Only one region was turned upright; its rows and the sheet's no longer share an
        # axis. The region-level row conservation contract still covers it.
        report.update(status=NOT_APPLICABLE, unaccounted=0,
                      note="A table on this sheet was turned upright, so rows are checked inside the table only.")
        return report

    if unaccounted:
        covered = _covered(lines, blank, results)
        # Lines logged from outside every region carry their own sheet position.
        covered |= {row["sheet_row"] - 1 for row in dropped if row.get("outside_region") and row.get("sheet_row")}
        report["unaccounted_rows"] = [
            {
                "sheet_row": index + 1,
                "content": " | ".join("" if is_blank(cell) else str(cell) for cell in lines[index]).strip(" |")[:200],
            }
            for index in range(len(lines))
            if index not in blank and index not in covered
        ][:50]
    return report


def _header_lines(result) -> int:
    header = result.trace.get("header") or {}
    if header.get("row_index") is None:
        return 0
    return 2 if header.get("multi_row") else 1


def _kept_source_lines(result) -> int:
    """Source rows behind the kept records; an unpivot turns one row into several."""
    pivot = result.trace.get("pivot") or {}
    if pivot.get("unpivoted"):
        return int(pivot.get("rows_before", 0))
    return len(result.frame)


def _covered(lines, blank: set[int], results) -> set[int]:
    """Sheet lines inside a detected table region (regions skip blank lines)."""
    covered: set[int] = set()
    for result in results:
        shape = result.trace.get("region_shape")
        origin = (result.trace.get("region_origin") or {}).get("row")
        if not shape or origin is None:
            continue
        remaining, index = shape[0], origin
        while remaining > 0 and index < len(lines):
            if index not in blank:
                covered.add(index)
                remaining -= 1
            index += 1
    return covered
