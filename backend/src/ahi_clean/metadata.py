"""Per-column metadata for a cleaned table, computed from the typed frame itself.

The metadata is built from the same polars frame that is written to the CSV, at the
moment it is written. That is the whole design: the cleaner has already decided every
column's type -- from values, never names -- and the frame's schema carries that
decision. Re-reading the CSV afterwards and re-guessing the types from text is how the
statistics and the stated datatype used to disagree, and how a column of codes that the
cleaner had deliberately kept as text ended up with a sum.

Polars' own schema inference is still run, on the written CSV, but as a *check*: it is
what a loader that infers types would see. Where it differs from ``datatype`` -- a code
column read as Int64 would lose its leading zeros -- the file should be loaded with the
stated ``datatype`` as an explicit schema.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl

from .orchestrate import SOURCE_SHEET_COLUMN

# Rows polars reads when inferring the written CSV's schema.
INFER_SCHEMA_LENGTH = 1000

# Written where a statistic does not apply -- the sum of a text column, the min of a
# column with no values -- so a reader sees "does not apply" rather than an ambiguous blank.
NOT_APPLICABLE = "NA"

FIELDNAMES = [
    "header_name",
    "datatype",
    "inferred_datatype",
    "distinct_count",
    "count",
    "total_row_count",
    "min",
    "max",
    "sum",
    "null_percentage",
    "excel_name",
    "sheet_name",
    "type_flag",
]

_SCHEMA = {
    name: (pl.Int64 if name in ("distinct_count", "count", "total_row_count") else pl.String)
    for name in FIELDNAMES
}


def build(
    frame: pl.DataFrame,
    excel_name: str,
    sheet_names: list[str],
    inferred: dict[str, str] | None = None,
    flags: dict[str, str] | None = None,
) -> pl.DataFrame:
    """One row per column, and per source sheet when the table was appended.

    An appended table is described sheet by sheet, so January and February can be
    compared side by side; ``sheet_name`` labels each block.
    """
    inferred = inferred or {}
    flags = flags or {}
    if SOURCE_SHEET_COLUMN in frame.columns:
        groups = [
            (str(key[0]), part)
            for key, part in frame.partition_by(
                SOURCE_SHEET_COLUMN, maintain_order=True, as_dict=True
            ).items()
        ]
    else:
        groups = [("; ".join(dict.fromkeys(sheet_names)), frame)]

    rows = []
    for sheet_name, part in groups:
        for column in frame.columns:
            row = describe(part[column])
            row.update(
                header_name=column,
                datatype=str(frame.schema[column]),
                # Every edge case on this column (flags.py): CHECK for a judgement call
                # to confirm against the header, INFO for a change made on purpose.
                # NA when there is nothing to say.
                type_flag=flags.get(column, NOT_APPLICABLE),
                inferred_datatype=inferred.get(column),
                excel_name=excel_name,
                sheet_name=sheet_name,
            )
            rows.append(row)
    return pl.DataFrame(rows, schema=_SCHEMA, orient="row") if rows else pl.DataFrame(schema=_SCHEMA)


def describe(series: pl.Series) -> dict:
    """The statistics for one column, chosen by its dtype.

    Numbers get min, max and sum; dates and datetimes get min and max; everything else
    gets neither. Counts and the null percentage apply to every type.
    """
    total = len(series)
    nulls = series.null_count()
    present = series.drop_nulls()
    row = {
        "distinct_count": present.n_unique(),
        "count": total - nulls,
        "total_row_count": total,
        "min": NOT_APPLICABLE,
        "max": NOT_APPLICABLE,
        "sum": NOT_APPLICABLE,
        "null_percentage": f"{(nulls / total * 100) if total else 0.0:.2f}",
    }
    if present.is_empty():
        return row

    dtype = series.dtype
    if dtype.is_numeric():
        row["min"] = _number_text(present.min())
        row["max"] = _number_text(present.max())
        row["sum"] = _exact_sum(present)
    elif dtype.is_temporal():
        row["min"] = str(present.min())
        row["max"] = str(present.max())
    return row


def _exact_sum(present: pl.Series) -> str:
    """Sum as a decimal, so money does not come out as 1234.5600000000002.

    Parsed from each value's text rather than cast from the float: a float-to-Decimal
    cast truncates, while polars renders a float as its shortest exact decimal text.

    Widened to the full 38 digits before summing. ``str.to_decimal`` infers the
    narrowest precision the values need, and the sum is kept inside that precision:
    10.5 + 21.0 + 31.5 + 42.0 + 52.5 came out as 158, not 157.5, with no error.
    """
    try:
        parsed = present.cast(pl.String).str.to_decimal()
        total = parsed.cast(pl.Decimal(38, parsed.dtype.scale)).sum()
        return _number_text(Decimal(str(total)))
    except (pl.exceptions.PolarsError, InvalidOperation):
        # Past Decimal's 38 digits of precision: fall back to Python's arbitrary one.
        return _number_text(sum((Decimal(str(value)) for value in present), Decimal(0)))


def _number_text(value) -> str:
    """A number as plain decimal text: no exponent, no trailing zeros."""
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def inferred_schema(csv_path: Path) -> dict[str, str]:
    """Each column's dtype as polars infers it from the first rows of the written CSV."""
    try:
        schema = pl.scan_csv(
            csv_path,
            infer_schema_length=INFER_SCHEMA_LENGTH,
            try_parse_dates=True,
        ).collect_schema()
    except (pl.exceptions.PolarsError, OSError):
        return {}
    return {name: str(dtype) for name, dtype in schema.items()}
