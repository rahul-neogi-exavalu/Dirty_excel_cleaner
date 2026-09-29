"""Explain the append outcome of a run: what combined, what could not, and why.

Computed inside the job from the tables it has just cleaned -- no extra cleaning pass.
The grouping mirrors ``orchestrate.plan_workbook``: tables append only when the *set* of
their cleaned column names matches exactly (``orchestrate.header_key``), and a table
without a header row of its own never appends. Where a table did not append, the report
lists the columns it is missing or has in addition, compared with the largest group.
"""

from __future__ import annotations

from ahi_clean import orchestrate


def build(results) -> dict:
    """Must be called before planning: planning renames adopted headerless tables."""
    tables = []
    for result in results:
        if result.frame.is_empty():
            continue
        tables.append(
            {
                "label": result.label,
                "sheet": result.sheet_name,
                "columns": list(result.frame.columns),
                "rows": len(result.frame),
                "header_detected": orchestrate.header_key(result) is not None,
            }
        )

    with_header = [table for table in tables if table["header_detected"]]
    headerless = [table for table in tables if not table["header_detected"]]

    grouped: dict[frozenset, list[dict]] = {}
    for table in with_header:
        grouped.setdefault(frozenset(table["columns"]), []).append(table)
    groups = sorted(grouped.values(), key=len, reverse=True)

    reference_columns = groups[0][0]["columns"] if groups else []
    reference_set = set(reference_columns)

    out_groups = []
    for index, group in enumerate(groups):
        columns = group[0]["columns"]
        present = set(columns)
        out_groups.append(
            {
                "tables": [table["label"] for table in group],
                "sheets": list(dict.fromkeys(table["sheet"] for table in group)),
                "columns": columns,
                "rows": sum(table["rows"] for table in group),
                "appended": len(group) > 1,
                "is_reference": index == 0,
                "missing_columns": [name for name in reference_columns if name not in present],
                "extra_columns": [name for name in columns if name not in reference_set],
            }
        )

    outputs = len(out_groups) + len(headerless)
    if not tables:
        status = "no_tables"
    elif len(tables) == 1:
        status = "single_table"
    elif outputs == 1:
        status = "all_match"
    elif any(group["appended"] for group in out_groups):
        status = "partial"
    else:
        status = "none_match"

    return {
        "status": status,
        "tables": len(tables),
        "outputs": outputs,
        "appended_groups": sum(1 for group in out_groups if group["appended"]),
        "groups": out_groups,
        "headerless": [
            {"label": table["label"], "sheet": table["sheet"], "columns": len(table["columns"]), "rows": table["rows"]}
            for table in headerless
        ],
    }
