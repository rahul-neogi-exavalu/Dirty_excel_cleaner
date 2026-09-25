"""Checks that must hold before a cleaned table is allowed to look trustworthy.

The rest of the pipeline *reports* what it did. These checks *refuse*. The difference
matters because the dangerous failure mode of a cleaner is not crashing -- it is emitting
a CSV that looks entirely plausible and is quietly wrong. A dropped record, a column of
nulls where the values slid one place left, a total that no longer reconciles: each of
those loads into a table without complaint and is found, if ever, months later.

Every check here is derived from evidence the pipeline already keeps, so none of them
require re-reading the source workbook.
"""

from __future__ import annotations

from dataclasses import dataclass

ROW_CONSERVATION = "row_conservation"
EMPTY_KEY_COLUMN = "empty_key_column"
UNEXPLAINED_EMPTY_COLUMN = "unexplained_empty_column"
TOTAL_RECONCILIATION = "total_reconciliation"
DUPLICATE_COLUMNS = "duplicate_columns"

SUM_TOLERANCE = 0.01


@dataclass
class Violation:
    """One broken contract, named so it can be acted on."""

    table: str
    check: str
    detail: str

    def as_dict(self) -> dict:
        return {"table": self.table, "check": self.check, "detail": self.detail}


def check_result(result) -> list[Violation]:
    """Run every contract against one extracted table."""
    violations: list[Violation] = []
    for check in (
        _row_conservation,
        _no_duplicate_columns,
        _key_columns_not_empty,
        _empty_columns_explained,
        _totals_reconcile,
    ):
        violations.extend(check(result))
    return violations


def check_all(results) -> list[Violation]:
    return [violation for result in results for violation in check_result(result)]


# --------------------------------------------------------------------------- #


def _row_conservation(result) -> list[Violation]:
    """Every row of the region must be accounted for: header, dropped, or kept.

    This is the check that catches a record vanishing. A row may only leave the output
    by being classified and logged with a reason; anything else is a bug, and without
    this check it is an invisible one.
    """
    trace = result.trace
    region = trace.get("region_shape")
    header = trace.get("header")
    if not region or header is None:
        return []

    header_index = header.get("row_index")
    if header_index is not None:
        header_rows = 2 if header.get("multi_row") else 1
        body = region[0] - header_index - header_rows
    else:
        # No header row in this region -- every row of it is body. This covers a genuinely
        # headerless table and one whose column names were adopted from a sibling sheet:
        # the names came from elsewhere, so there is still no header line to skip here.
        header_index, header_rows = -1, 0
        body = region[0]

    # Only drops that fall inside the body are subtracted. Banner rows sit *above* the
    # header and are already excluded by skipping past it, so counting them here would
    # subtract them twice and report a loss that never happened.
    body_drops = [
        row
        for row in trace.get("dropped_rows", [])
        if row.get("region_row") is None or row["region_row"] > header_index + header_rows - 1
    ]
    expected = body - len(body_drops)

    pivot = trace.get("pivot")
    if pivot and pivot.get("unpivoted"):
        # A melt turns each value cell into a row, so conservation is measured against
        # cells rather than rows -- nothing lost, nothing invented.
        cells = pivot["rows_before"] * len(pivot["value_columns"])
        if len(result.frame) != cells:
            return [
                Violation(
                    result.label,
                    ROW_CONSERVATION,
                    f"unpivot produced {len(result.frame)} rows from {cells} value cells",
                )
            ]
        return []

    if expected < 0:
        return []  # region bookkeeping not applicable (headerless or reshaped)
    if len(result.frame) != expected:
        return [
            Violation(
                result.label,
                ROW_CONSERVATION,
                f"{len(result.frame)} rows kept but {expected} expected "
                f"({body} body rows less {len(body_drops)} dropped inside the body)",
            )
        ]
    return []


def _no_duplicate_columns(result) -> list[Violation]:
    """Duplicate names make a table unloadable, whatever else is right about it.

    Kept as defence in depth rather than as the guard. polars refuses to construct a
    frame with repeated column names at all, so this cannot fire through the normal
    path -- the guarantee moved out of a runtime check and into the type system when the
    dataframe library changed. It stays because it is free and because a future
    construction path that bypasses polars' own check should still be caught.
    """
    names = list(result.frame.columns)
    duplicated = {name for name in names if names.count(name) > 1}
    if duplicated:
        return [Violation(result.label, DUPLICATE_COLUMNS, f"repeated: {sorted(duplicated)}")]
    return []


def _key_columns_not_empty(result) -> list[Violation]:
    """A key column that came out entirely null means alignment failed.

    This is the signature of the misalignment class where the body sits one column left
    of its header: the labels are all present and every value is under the wrong one.

    Which columns are keys is read from the types recorded at coercion time, not from
    the values still present. Inferring it from the surviving values cannot work -- an
    all-null column has nothing left to identify it by, so the check would be incapable
    of ever firing, which is exactly the flaw it exists to catch elsewhere.
    """
    from . import typing_utils

    frame = result.frame
    if frame.is_empty():
        return []

    inferred = result.trace.get("inferred_types", {})
    violations = []
    for position, name in enumerate(frame.columns):
        if inferred.get(name) != typing_utils.ID_STRING:
            continue
        if _column_at(frame, position).null_count() == frame.height:
            violations.append(
                Violation(result.label, EMPTY_KEY_COLUMN, f"key column '{name}' is entirely null")
            )
    return violations


def _column_at(frame, position):
    """A column by position, so duplicate names cannot make the lookup ambiguous."""
    return frame.to_series(position)


def _empty_columns_explained(result) -> list[Violation]:
    """An all-null column must have a stated cause.

    Either the source column really was blank, or it is formula-driven with no cached
    result. Anything else -- particularly several columns emptying at once -- is the
    fingerprint of a structural error, not of missing data.
    """
    frame = result.frame
    if frame.is_empty():
        return []
    explained = set(result.trace.get("empty_columns", []))
    unexplained = [
        name
        for position, name in enumerate(frame.columns)
        if _column_at(frame, position).null_count() == frame.height and name not in explained
    ]
    if not unexplained:
        return []
    return [
        Violation(
            result.label,
            UNEXPLAINED_EMPTY_COLUMN,
            f"no stated cause for empty column(s): {sorted(unexplained)}",
        )
    ]


def _totals_reconcile(result) -> list[Violation]:
    """A total we removed must agree with the rows we kept.

    The strongest check available and effectively free: the figures are already retained
    in the audit trail, so the discarded rows verify the kept ones. A grand total that
    covered every record is the only one checked here, because a subtotal's scope --
    which rows it summed -- is not recoverable from the CSV alone.
    """
    frame = result.frame
    if frame.is_empty():
        return []
    totals = [
        row
        for row in result.trace.get("dropped_rows", [])
        if row.get("classification") == "GRAND_TOTAL"
    ]
    if not totals:
        return []

    violations = []
    for row in totals:
        declared = _figures_in(row.get("content", ""))
        if not declared:
            continue
        if not any(_matches_a_column_sum(frame, value) for value in declared):
            violations.append(
                Violation(
                    result.label,
                    TOTAL_RECONCILIATION,
                    f"removed grand total {declared} matches no column sum of the kept rows",
                )
            )
    return violations


def _figures_in(content: str) -> list[float]:
    figures = []
    for part in content.split("|"):
        try:
            figures.append(float(part.strip().replace(",", "")))
        except ValueError:
            continue
    return figures


def _matches_a_column_sum(frame, target: float) -> bool:
    for position in range(len(frame.columns)):
        column = _column_at(frame, position)
        if not column.dtype.is_numeric():
            continue
        values = column.drop_nulls()
        if values.is_empty():
            continue
        if abs(float(values.sum()) - target) <= SUM_TOLERANCE:
            return True
    return False
