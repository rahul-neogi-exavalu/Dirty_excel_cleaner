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

ROW_ACCOUNTING = "row_accounting"

CHECKS = [
    (ROW_ACCOUNTING, "Every source row is accounted for",
     "Raw rows must equal blank + header + removed (with a reason) + kept. A leftover row was dropped silently."),
    (contracts.ROW_CONSERVATION, "No rows lost inside the table",
     "Inside the table, every row must be the header, a logged removal, or kept."),
    (contracts.TOTAL_RECONCILIATION, "Removed totals match the kept rows",
     "A grand total that was removed must equal the sum of the rows that were kept."),
    (contracts.EMPTY_KEY_COLUMN, "Key columns are filled",
     "An ID column that comes out entirely empty means values shifted away from their header."),
    (contracts.UNEXPLAINED_EMPTY_COLUMN, "Empty columns have a known cause",
     "A fully empty column must be blank in the source or formula-driven; otherwise it is a structural error."),
    (contracts.DUPLICATE_COLUMNS, "Column names are unique",
     "Repeated column names make a table impossible to load."),
]

PASSED, FAILED, NOT_APPLICABLE = "passed", "failed", "not_applicable"


def build(grids, results, outputs, violations: list[dict]) -> dict[str, dict]:
    """One report per output, keyed by the output's job id."""
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
        reports[output.job_id] = _report(output.job_id, tables, sheets, failures)
    return reports


def _report(output_id: str, tables: list, sheets: list[dict], failures: list[dict]) -> dict:
    checks = []
    for check_id, title, description in CHECKS:
        if check_id == ROW_ACCOUNTING:
            applicable = [sheet for sheet in sheets if sheet["status"] != NOT_APPLICABLE]
            bad = [sheet for sheet in applicable if sheet["status"] == FAILED]
            details = [
                f"{sheet['sheet']}: {_count(abs(sheet['unaccounted']), sheet['axis'])} "
                + ("not accounted for" if sheet["unaccounted"] > 0 else "more in the output than in the source")
                for sheet in bad
            ]
            status = FAILED if bad else PASSED if applicable else NOT_APPLICABLE
        else:
            found = [item for item in failures if item["check"] == check_id]
            details = [f"{item['table']}: {item['detail']}" for item in found]
            status = FAILED if found else PASSED
            if not found and not _applies(check_id, tables):
                status = NOT_APPLICABLE
        checks.append({"id": check_id, "title": title, "description": description,
                       "status": status, "details": details})

    issues = sum(1 for check in checks if check["status"] == FAILED)
    return {
        "output_id": output_id,
        "status": FAILED if issues else PASSED,
        "issues": issues,
        "checks": checks,
        "accounting": sheets,
    }


def _count(number: int, axis: str) -> str:
    """'1 row', '3 rows' -- axis is the plural ('rows' / 'columns')."""
    return f"{number} {axis[:-1] if number == 1 else axis}"


def _applies(check_id: str, tables: list) -> bool:
    """Whether a passing check had anything to check, so 'passed' is not claimed vacuously."""
    if check_id == contracts.TOTAL_RECONCILIATION:
        return any(row.get("classification") == "GRAND_TOTAL"
                   for table in tables for row in table.trace.get("dropped_rows", []))
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

    blank = {index for index, line in enumerate(lines) if all(is_blank(cell) for cell in line)}
    header = sum(_header_lines(result) for result in results if not result.frame.is_empty())
    dropped = [row for result in results for row in result.trace.get("dropped_rows", [])]
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
