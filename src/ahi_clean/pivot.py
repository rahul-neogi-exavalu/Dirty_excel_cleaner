"""Detect a pivoted (wide) table and melt it back to long form.

A pivot matrix is not a row-classification problem and not an orientation problem. Its
columns are not *fields* at all -- they are values of one variable, spread sideways, with
a single measure filling the grid:

    Producer   | Jan-2026 | Feb-2026 | Mar-2026
    Pinnacle   | 12299.66 | 35117.88 | 32440.79

Read as an ordinary table it yields a column per month, which no downstream table can
use. Worse, it is genuinely *symmetric* -- every row is homogeneous and so is every
column -- so orientation detection has nothing to grip on and will flip it on a
tiebreak. Pivots therefore have to be recognised before orientation is decided.

Detection is structural: a run of trailing columns whose *headers* are all values of one
kind, heading cells that are all one measure.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from . import signals, typing_utils
from .typing_utils import is_blank

MIN_VALUE_COLUMNS = 3
MIN_ID_COLUMNS = 1
VALUE_PURITY = 0.9

# 'Jan-2026' is not a date by the lattice's reckoning -- it has no ISO shape and reads
# as an identifier. The axis of a pivot is therefore recognised by behaviour rather than
# by type name: a run of same-typed, all-distinct labels. On its own that is far too
# loose, so it is only ever accepted together with the two guards below -- the cells
# beneath must be a single numeric measure, and the remaining columns must uniquely
# identify each row. An ordinary table fails both.
_DATE_AXIS = {typing_utils.DATE, typing_utils.DATETIME, typing_utils.DATE_STRING}


@dataclass
class Pivot:
    """A detected wide layout: which columns identify a row, which hold the measure."""

    header_index: int
    id_columns: list[int]
    value_columns: list[int]
    variable_kind: str

    @property
    def variable_name(self) -> str:
        """A name for the melted key, taken from what the headers actually are.

        Inferred from the values themselves -- headers that resolve to points in time
        make a period axis -- never from a list of expected field names.
        """
        return "period" if self.variable_kind == "period" else "category"


def detect(rows, header_index: int) -> Pivot | None:
    """Find a pivot layout in a block whose header row is already known."""
    if header_index is None or header_index >= len(rows):
        return None

    header = rows[header_index]
    body = rows[header_index + 1 :]
    if len(body) < 2 or len(header) < MIN_ID_COLUMNS + MIN_VALUE_COLUMNS:
        return None

    # Walk in from the right while the header cells keep telling the same story.
    kinds = [typing_utils.infer_type(cell) for cell in header]
    if is_blank(header[-1]):
        return None
    tail_kind = kinds[-1]

    value_columns: list[int] = []
    for index in range(len(header) - 1, -1, -1):
        if kinds[index] != tail_kind or is_blank(header[index]):
            break
        value_columns.append(index)
    value_columns.reverse()

    if len(value_columns) < MIN_VALUE_COLUMNS:
        return None
    id_columns = [index for index in range(len(header) - len(value_columns))]
    if len(id_columns) < MIN_ID_COLUMNS:
        return None

    labels = [str(header[index]).strip() for index in value_columns]
    if len(set(labels)) != len(labels):
        return None
    # The identifying columns must not be the same kind as the axis; if every header on
    # the row reads alike, this is an ordinary table, not a matrix.
    if all(kinds[index] == tail_kind for index in id_columns):
        return None

    # The headed cells must be one measure, not a mixture. A pivot grid holds numbers
    # of a single kind; anything else is an ordinary table that happens to have
    # date-like column names.
    if not _is_one_measure(body, value_columns):
        return None
    # The identifying columns must actually identify: a pivot has one row per entity.
    if not _identifies_rows(body, id_columns):
        return None

    axis = "period" if tail_kind in _DATE_AXIS or _all_parse_as_dates(labels) else "category"
    return Pivot(
        header_index=header_index,
        id_columns=id_columns,
        value_columns=value_columns,
        variable_kind=axis,
    )


def _all_parse_as_dates(labels) -> bool:
    """Whether every heading resolves to a point in time -- a date or a month."""
    from . import coerce

    return coerce.looks_like_periods(labels)


def _is_one_measure(body, value_columns) -> bool:
    values = [
        row[index]
        for row in body
        for index in value_columns
        if index < len(row) and not is_blank(row[index])
    ]
    if not values:
        return False
    kinds = {typing_utils.infer_type(value) for value in values}
    numeric = {typing_utils.INT, typing_utils.FLOAT}
    if not kinds <= numeric:
        return False
    filled = len(values) / (len(body) * len(value_columns))
    return filled >= VALUE_PURITY


def _identifies_rows(body, id_columns) -> bool:
    keys = [
        tuple(
            "" if index >= len(row) or is_blank(row[index]) else str(row[index]).strip()
            for index in id_columns
        )
        for row in body
    ]
    keys = [key for key in keys if any(part for part in key)]
    return bool(keys) and len(set(keys)) == len(keys)


def looks_pivoted(rows) -> bool:
    """Cheap check used before orientation is decided.

    Orientation must not run on a pivot: the block is symmetric, so the vote falls to a
    shape tiebreak and flips a perfectly readable matrix on its side. This asks only
    whether the first populated row heads a run of like-typed columns -- enough to hold
    the flip back, with the full check done later once the header is known.
    """
    for index, row in enumerate(rows[:5]):
        if signals.fill_count(row) < MIN_ID_COLUMNS + MIN_VALUE_COLUMNS:
            continue
        if detect(rows, index) is not None:
            return True
    return False


def melt(
    frame: pl.DataFrame, pivot: Pivot, names: list[str], labels: list | None = None
) -> tuple[pl.DataFrame, dict]:
    """Unpivot a wide frame into long form.

    ``Producer | Jan | Feb`` becomes ``producer | period | value``, one row per cell,
    which is the shape a table can actually take.
    """
    id_names = [names[index] for index in pivot.id_columns if index < len(names)]
    value_names = [names[index] for index in pivot.value_columns if index < len(names)]
    id_names = [name for name in id_names if name in frame.columns]
    value_names = [name for name in value_names if name in frame.columns]
    if not id_names or not value_names:
        return frame, {"unpivoted": False}

    long = frame.unpivot(
        index=id_names,
        on=value_names,
        variable_name=pivot.variable_name,
        value_name="value",
    ).drop_nulls("value")

    # The melted key should carry what the sheet actually said -- 'Jan-2026', not the
    # SQL-safe name the column was given. Those labels are data now, not identifiers.
    if labels is not None:
        original = {
            names[index]: str(labels[index]).strip()
            for index in pivot.value_columns
            if index < len(names) and index < len(labels) and not is_blank(labels[index])
        }
        long = long.with_columns(
            pl.col(pivot.variable_name).replace(original).alias(pivot.variable_name)
        )

    report = {
        "unpivoted": True,
        "id_columns": id_names,
        "value_columns": value_names,
        "variable_column": pivot.variable_name,
        "rows_before": int(len(frame)),
        "rows_after": int(len(long)),
    }
    return long, report
