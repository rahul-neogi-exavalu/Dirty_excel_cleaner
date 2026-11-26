"""Transactions, or figures the profit center already aggregated?

Some profit centers send summaries -- premium per partner and product line, a month to a
sheet -- instead of their transactions. Those belong in Silver's aggregate table, not
its transaction table, and are told apart by the table's shape, never by its names:

* **Totals.** A column that is the sum of two or more others on (almost) every row -- a
  TOTALS column across product lines -- or rows that are the sum of the other rows of
  their sheet (a TOTALS row).
* **Measures.** At least as many amount columns as text columns.
* **No transactions.** Nothing identifies one: no column is the policy number, and no
  transaction date is populated row by row.

All three make a table aggregated. The reviewer can say otherwise, either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

TRANSACTION, AGGREGATE = "transaction", "aggregate"
# A column counts as an amount when this share of its values reads as a number.
NUMERIC_SHARE = 0.95
# A total column or row holds on this share of the rows (a stray rounding is allowed).
TOTAL_SHARE = 0.95


@dataclass
class Grain:
    aggregated: bool
    # Each signal: {id, label, ok, detail}, as the reviewer reads it.
    signals: list[dict] = field(default_factory=list)
    # The column that totals others across the row, and the columns it totals.
    total_column: str | None = None
    parts: list[str] = field(default_factory=list)
    # Rows that total the other rows of their sheet (frame row positions).
    total_rows: list[int] = field(default_factory=list)
    measures: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"aggregated": self.aggregated, "signals": self.signals, "total_column": self.total_column,
                "parts": self.parts, "total_rows": len(self.total_rows), "measures": self.measures,
                "dimensions": self.dimensions}


def numbers(series: pl.Series) -> pl.Series:
    """The values as floats; what does not read as a number (or is blank) is null."""
    if series.dtype.is_numeric():
        return series.cast(pl.Float64)
    text = series.cast(pl.String).str.strip_chars().str.replace_all(",", "")
    return text.cast(pl.Float64, strict=False)


def measure_columns(frame: pl.DataFrame, columns: list[str]) -> list[str]:
    """The columns whose populated values are (almost) all numbers."""
    found = []
    for name in columns:
        raw = frame[name]
        present = int(raw.cast(pl.String).str.strip_chars().replace("", None).is_not_null().sum())
        if present and int(numbers(raw).is_not_null().sum()) >= NUMERIC_SHARE * present:
            found.append(name)
    return found


def _close(left: pl.Series, right: pl.Series) -> pl.Series:
    """Equal within a cent, or a millionth of the amount (spreadsheet rounding)."""
    tolerance = pl.max_horizontal(pl.lit(0.01), right.abs() * 1e-6)
    return pl.DataFrame({"l": left, "r": right}).select(((pl.col("l") - pl.col("r")).abs() <= tolerance))[:, 0]


def total_column(frame: pl.DataFrame, measures: list[str]) -> tuple[str, list[str]] | None:
    """A measure that is the sum of two or more others, row by row: all the other
    measures, or the run of measures just before it (a total at the end of a group)."""
    values = {name: numbers(frame[name]) for name in measures}
    for index, name in enumerate(measures):
        others = [other for other in measures if other != name]
        before = measures[:index]
        for parts in dict.fromkeys(tuple(group) for group in (others, before) if len(group) >= 2):
            total = values[name]
            summed = pl.DataFrame({part: values[part].fill_null(0) for part in parts}).sum_horizontal()
            rows = total.is_not_null() & (total.abs() > 0)
            if int(rows.sum()) < 2:
                continue
            matched = int((_close(summed, total.fill_null(0)) & rows).sum())
            if matched >= TOTAL_SHARE * int(rows.sum()):
                return name, list(parts)
    return None


def total_rows(frame: pl.DataFrame, measures: list[str], group: str | None = None) -> list[int]:
    """Rows whose every amount is the sum of the other rows of their group (sheet): a
    TOTALS row under the records."""
    if not measures or frame.height < 3:
        return []
    data = pl.DataFrame({name: numbers(frame[name]).fill_null(0) for name in measures}).with_row_index("_row")
    keys = frame[group] if group and group in frame.columns else pl.Series([None] * frame.height)
    data = data.with_columns(keys.alias("_group"))
    found = []
    for _, part in data.group_by("_group", maintain_order=True):
        if part.height < 3:
            continue
        sums = {name: float(part[name].sum()) for name in measures}
        totals = []
        for row in part.iter_rows(named=True):
            amounts = [row[name] for name in measures]
            if not any(amounts):
                continue
            # The row equals the sum of the others: twice it is the group's sum.
            if all(abs(2 * row[name] - sums[name]) <= max(0.01, abs(sums[name]) * 1e-6) for name in measures):
                totals.append(int(row["_row"]))
        # A sheet with one record that is not zero has it equal to the total too: the
        # total is the last of them, under the records.
        if totals:
            found.append(totals[-1])
    return found


def detect(frame: pl.DataFrame, columns: list[str], *, keyed: bool, dated: bool, group: str | None = None) -> Grain:
    """Whether a cleaned table holds aggregated figures. ``columns``: the table's own
    columns (not the cleaner's sheet column); ``keyed``: a column is the policy number;
    ``dated``: a transaction date is populated on every row; ``group``: the column naming
    each row's sheet, where sheets are separate periods."""
    measures = measure_columns(frame, columns)
    dimensions = [name for name in columns if name not in measures]
    totals = total_column(frame, measures)
    rows = total_rows(frame, [name for name in measures if not totals or name != totals[0]], group)
    heavy = bool(measures) and len(measures) >= len(dimensions)
    signals = [
        {"id": "totals", "label": "Totals", "ok": bool(totals or rows),
         "detail": (f"{totals[0]} is the sum of {', '.join(totals[1])}" if totals else "")
         + ("; " if totals and rows else "")
         + ((f"{len(rows)} rows total the rows of their sheet" if len(rows) != 1
             else "1 row totals the rows of its sheet") if rows else "")
         or "No column or row totals the others"},
        {"id": "measures", "label": "Mostly amounts", "ok": heavy,
         "detail": f"{len(measures)} amount column{'s' if len(measures) != 1 else ''}, "
                   f"{len(dimensions)} text column{'s' if len(dimensions) != 1 else ''}"},
        {"id": "no_key", "label": "No transaction key", "ok": not keyed,
         "detail": "A column is the policy number" if keyed else "No column is the policy number"},
        {"id": "no_dates", "label": "No transaction dates", "ok": not dated,
         "detail": "A transaction date is populated on every row" if dated else "No date column is populated row by row"},
    ]
    aggregated = bool(totals or rows) and heavy and not keyed and not dated
    return Grain(aggregated, signals, totals[0] if totals else None, list(totals[1]) if totals else [], rows,
                 measures, dimensions)
