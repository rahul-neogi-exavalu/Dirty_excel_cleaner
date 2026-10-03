"""One bronze table's rows -> silver_detail rows, under an approved mapping.

Used twice with the same code: for the dry-run quality report the reviewer sees before
approving, and for the load itself -- so what was reviewed is what is written.

The result has exactly the catalog's columns (the business's silver_schema), minus the
generated key ``ahi_policy_transaction_id``:

* mapped columns: the bronze column(s) mapped onto them, cleansed to their type. One
  bronze column may feed several Silver columns (the DRT mapping does this, e.g. one
  "EffectiveDate" for both the policy and the accounting date);
* profit_center_name / profit_center_number: completed from the LOTL (the document's
  four cases);
* policy_effective_year / month: taken from policy_effective_date when not mapped;
* system columns: source_system, source_table, source_file, ingestion_timestamp (the
  bronze load's processing_date), processed / insert / update timestamps, the source
  data period, and the business-key and row hashes.

Rows of one bronze load are identified in Silver by (source_table, source_file,
ingestion_timestamp); there are no internal lineage columns.
"""

from __future__ import annotations

import calendar
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

import polars as pl

from . import cleanse, hashing, profit_center
from .catalog import MAPPED, SilverColumn

NAME, NUMBER = "profit_center_name", "profit_center_number"
IDENTITY = "ahi_policy_transaction_id"
# What the bronze rows are read with, besides their data columns.
LINEAGE = ["_ingestion_id", "_source_file", "_source_sheet"]
# What ``transform(..., extras=True)`` adds after the catalog columns, for the cleansed table.
EXTRAS = ["_ingestion_id", "_pc_status", "_invalid_columns"]


@dataclass
class LoadInfo:
    """One bronze load, from the ingestion audit table."""

    file_name: str | None = None
    processing_date: datetime | None = None
    period_start: str | None = None  # YYYY-MM
    period_end: str | None = None
    # The load's own profit center (PC0796): a table can hold loads of several.
    pc_id: str | None = None


@dataclass
class Context:
    source_system: str | None = None
    source_table: str | None = None
    processed_at: datetime | None = None
    # _ingestion_id -> its load
    loads: dict[str, LoadInfo] = field(default_factory=dict)


def _pairs(mapping) -> list[tuple[str, str]]:
    """(bronze, silver) pairs from {bronze: silver}, {bronze: [silver, ...]} or pairs."""
    if isinstance(mapping, dict):
        pairs = []
        for bronze, silver in mapping.items():
            for target in (silver if isinstance(silver, (list, tuple)) else [silver]):
                if target:
                    pairs.append((bronze, target))
        return pairs
    return [(bronze, silver) for bronze, silver in mapping if silver]


def _period(start: str | None, end: str | None) -> tuple[date | None, date | None, str | None]:
    """'2026-01'..'2026-06' -> (2026-01-01, 2026-06-30, 'DATE_RANGE'); one month -> 'MONTH'."""
    def first(text):
        try:
            year, month = (int(part) for part in text.split("-")[:2])
            return date(year, month, 1)
        except (AttributeError, ValueError):
            return None

    begin, finish = first(start), first(end or start)
    if begin is None or finish is None:
        return None, None, None
    last = date(finish.year, finish.month, calendar.monthrange(finish.year, finish.month)[1])
    return begin, last, "MONTH" if (begin.year, begin.month) == (finish.year, finish.month) else "DATE_RANGE"


def transform(
    frame: pl.DataFrame,
    mapping,
    catalog: list[SilverColumn],
    pc: str | None,
    lotl: profit_center.Lotl,
    context: Context | None = None,
    extras: bool = False,
) -> tuple[pl.DataFrame, dict]:
    """``frame``: bronze rows as text (data columns plus LINEAGE). ``mapping``: which bronze
    column feeds which Silver column (see ``_pairs``). ``pc``: the loads' pc_id (PC0796),
    used for a row whose load does not name its own.

    Returns the Silver frame (catalog columns in order, without the generated key) and a
    quality report. With ``extras``, the frame also ends with EXTRAS: each row's load,
    how its profit center was settled, and which of its values could not be read.
    """
    context = context or Context()
    height = frame.height
    source_for: dict[str, str] = {}
    for bronze, silver in _pairs(mapping):
        source_for.setdefault(silver, bronze)
    columns: dict[str, pl.Series] = {}
    invalid: dict[str, int] = {}
    unreadable: dict[str, pl.Series] = {}
    mapped = [column for column in catalog if column.role == MAPPED]
    for column in mapped:
        bronze = source_for.get(column.name)
        series = frame[bronze] if bronze in frame.columns else pl.Series(column.name, [None] * height, dtype=pl.String)
        typed, failures = cleanse.by_type(series.alias(column.name), column)
        columns[column.name] = typed
        if failures:
            invalid[column.name] = failures
            unreadable[column.name] = cleanse.text(series).is_not_null() & typed.is_null()

    ids = frame["_ingestion_id"].to_list() if "_ingestion_id" in frame.columns else [None] * height
    loads = [context.loads.get(i) or LoadInfo() for i in ids]

    # Profit center: the document's four scenarios, row by row, each row by its own load.
    statuses: list[str | None] = [None] * height
    if NAME in columns or NUMBER in columns:
        names = columns[NAME].to_list() if NAME in columns else [None] * height
        numbers = columns[NUMBER].to_list() if NUMBER in columns else [None] * height
        resolved = [profit_center.resolve(n, m, load.pc_id or pc, lotl) for n, m, load in zip(names, numbers, loads)]
        if NAME in columns:
            columns[NAME] = pl.Series(NAME, [r[0] for r in resolved], dtype=columns[NAME].dtype)
        if NUMBER in columns:
            columns[NUMBER] = pl.Series(NUMBER, [r[1] for r in resolved], dtype=columns[NUMBER].dtype)
        statuses = [r[2] for r in resolved]

    # Year and month of the policy date, when the source does not give them.
    if "policy_effective_date" in columns:
        when = columns["policy_effective_date"]
        for name, part in (("policy_effective_year", when.dt.year()), ("policy_effective_month", when.dt.month())):
            if name in columns:
                columns[name] = columns[name].fill_null(part.cast(columns[name].dtype))

    # Hashes over the business columns, before any system column is added.
    names = [column.name for column in mapped]
    rows = pl.DataFrame({name: columns[name] for name in names}).rows() if names else [()] * height
    key_index = [names.index(column.name) for column in mapped if column.business_key]
    business = [hashing.digest([row[i] for i in key_index]) if key_index else None for row in rows]
    whole = [hashing.digest(row) for row in rows]

    files = frame["_source_file"].to_list() if "_source_file" in frame.columns else [None] * height
    periods = [_period(load.period_start, load.period_end) for load in loads]
    system: dict[str, list] = {
        "source_system": [context.source_system] * height,
        "source_table": [context.source_table] * height,
        "business_key_hash": business,
        "row_hash": whole,
        "source_file": [load.file_name or name for load, name in zip(loads, files)],
        "ingestion_timestamp": [load.processing_date for load in loads],
        "processed_timestamp": [context.processed_at] * height,
        "silver_insert_timestamp": [context.processed_at] * height,
        "silver_update_timestamp": [context.processed_at] * height,
        "source_data_period_start_date": [p[0] for p in periods],
        "source_data_period_end_date": [p[1] for p in periods],
        "source_data_period_type": [p[2] for p in periods],
    }
    ordered = []
    for column in catalog:
        if column.name == IDENTITY:
            continue
        if column.name in columns:
            ordered.append(columns[column.name])
        elif column.name in system:
            ordered.append(_series(column, system[column.name]))
        else:  # a system column nothing derives yet (drt_reporting_*): NULL
            ordered.append(_series(column, [None] * height))
    if extras:
        ordered += [
            pl.Series("_ingestion_id", ids, dtype=pl.String),
            pl.Series("_pc_status", statuses, dtype=pl.String),
            _unreadable_names(unreadable, height),
        ]
    silver = pl.DataFrame(ordered) if ordered else pl.DataFrame()

    quality = {
        "rows": height,
        "invalid_values": invalid,
        "profit_center": dict(Counter(status for status in statuses if status)),
        "unmapped_silver_columns": [name for name in names if name not in source_for],
    }
    return silver, quality


def _unreadable_names(unreadable: dict[str, pl.Series], height: int) -> pl.Series:
    """Per row, the Silver columns whose value was there but could not be read."""
    if not unreadable:
        return pl.Series("_invalid_columns", [[] for _ in range(height)], dtype=pl.List(pl.String))
    flags = pl.DataFrame(unreadable)
    names = flags.select(pl.concat_list(
        [pl.when(pl.col(name)).then(pl.lit(name)).otherwise(pl.lit(None, dtype=pl.String)) for name in unreadable]
    ).list.drop_nulls().alias("_invalid_columns"))
    return names.to_series()


_DTYPES = {"string": pl.String, "int": pl.Int32, "bigint": pl.Int64, "boolean": pl.Boolean, "date": pl.Date,
           "timestamp": pl.Datetime("us", "UTC")}


def _series(column: SilverColumn, values: list) -> pl.Series:
    if column.kind == "decimal":
        dtype = pl.Decimal(column.precision, column.scale)
    else:
        dtype = _DTYPES[column.kind]
    if column.kind == "timestamp":
        values = [_utc(value) for value in values]
    return pl.Series(column.name, values, dtype=dtype)


def _utc(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        from datetime import timezone

        return value.replace(tzinfo=timezone.utc)
    return value
