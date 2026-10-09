"""A profit center's own aggregates -> silver_aggregate rows, under an approved mapping.

Some profit centers send summaries instead of transactions -- premium per delegated
authority partner and product line, a month to a sheet. Those rows go to Silver's
aggregate table as the profit center reported them (never through the transaction
table, never summed again). Used for the reviewer's dry run and for the load, like
``transform``:

* **dimension columns** (a partner, a carrier) -> the aggregate column they are mapped
  to, and in order into ``aggregation_dimension_1..3`` (name and value);
* **measure columns** (premium, a policy count) -> their measure;
* **a measure spread across a dimension** -- Casualty Treaty, Property Treaty, Workers
  Comp, each a premium -- is unpivoted: one row per cell, the measure from the cell and
  the dimension (``product_line_name``, or the next ``aggregation_dimension``) from the
  column's header;
* **roll-ups are left out**: a column totalling others (TOTALS) is not mapped, and a row
  totalling the other rows of its sheet is dropped -- Silver holds the figures, and sums
  them itself;
* **system columns**: record grain, report type, data source, the profit center (from
  the load, completed from the LOTL), the reporting month (each row's sheet month, else
  the load's reporting dates), lineage, hashes and timestamps.
"""

from __future__ import annotations

import calendar
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from ahi_bronze import grain

from . import cleanse, hashing, profit_center
from .catalog import MAPPED, SilverColumn
from .transform import Context, LoadInfo, _period, _series, _unreadable_names

IDENTITY = "ahi_aggregate_id"
RECORD_GRAIN = "AS_REPORTED"
REPORT_TYPE = "SOURCE_AGGREGATE"
DATA_SOURCE = "bronze_aggregate"
# Each row's month, as the bronze table keeps it (the sheet it came from).
MONTH = "_reporting_month"
LINEAGE = ["_ingestion_id", "_source_file", "_source_sheet", MONTH]
EXTRAS = ["_ingestion_id", "_pc_status", "_invalid_columns"]
SLOTS = 3
# Filled by the load, never mapped from a bronze column.
SYSTEM = frozenset({
    IDENTITY, "record_grain", "report_type", "agg_data_source", "source_system", "source_table",
    "profit_center_number", "profit_center_name", "aggregation_category",
    *(f"aggregation_dimension_{n}_{part}" for n in range(1, SLOTS + 1) for part in ("name", "value")),
    "reporting_start_date", "reporting_end_date", "reporting_year", "reporting_month", "reporting_period",
    "reporting_period_type", "accounting_effective_date", "business_key_hash", "row_hash", "source_file",
    "source_sheet", "file_date", "ingestion_timestamp", "processed_timestamp", "silver_insert_timestamp",
    "silver_update_timestamp", "source_data_period_start_date", "source_data_period_end_date",
    "source_data_period_type", *(f"measure_{n}_{part}" for n in range(1, 4) for part in ("name", "value")),
})


@dataclass
class Spread:
    """Columns that are one measure across a dimension: their headers are its values."""

    columns: list[str] = field(default_factory=list)
    measure: str | None = None  # the aggregate measure the cells are (premium)
    dimension: str | None = None  # the aggregate column the headers fill (product_line_name)
    label: str = "category"  # the name an aggregation_dimension slot records, when no column is named

    def as_dict(self) -> dict:
        return {"columns": self.columns, "measure": self.measure, "dimension": self.dimension, "label": self.label}

    @classmethod
    def from_dict(cls, data: dict | None) -> Spread | None:
        if not data:
            return None
        return cls(list(data.get("columns") or []), data.get("measure"), data.get("dimension"),
                   data.get("label") or "category")


def targets(catalog: list[SilverColumn]) -> list[SilverColumn]:
    """The aggregate columns a bronze column can be mapped onto, as mappable catalog
    columns: every one the load does not fill itself."""
    return [SilverColumn(column.name, column.name.replace("_", " "), column.data_type, False, column.description, MAPPED)
            for column in catalog if column.name not in SYSTEM]


def is_measure(column: SilverColumn) -> bool:
    """An amount or a count: what a row reports, not what it is grouped by."""
    return column.kind in ("decimal", "bigint")


def rollup_rows(frame: pl.DataFrame, measures: list[str]) -> list[int]:
    """Rows totalling the other rows of their sheet (a TOTALS row): left out."""
    present = [name for name in measures if name in frame.columns]
    return grain.total_rows(frame, present, "_source_sheet" if "_source_sheet" in frame.columns else None)


def transform(
    frame: pl.DataFrame,
    mapping: list[tuple[str, str]],
    spread: Spread | None,
    catalog: list[SilverColumn],
    pc: str | None,
    lotl: profit_center.Lotl,
    context: Context | None = None,
    headers: dict[str, str] | None = None,
    extras: bool = False,
) -> tuple[pl.DataFrame, dict]:
    """``frame``: bronze rows as text (data columns plus LINEAGE). ``mapping``: (bronze,
    aggregate column) pairs for the dimensions and single measures. ``spread``: columns
    to unpivot. ``headers``: bronze column -> the header the file wrote (a spread
    column's header is its dimension value). Returns the aggregate rows (catalog columns
    without the generated key) and a quality report."""
    context = context or Context()
    headers = headers or {}
    by_name = {column.name: column for column in catalog}
    pairs = [(bronze, target) for bronze, target in mapping if target in by_name and bronze in frame.columns]
    spread_columns = [name for name in (spread.columns if spread else []) if name in frame.columns]
    measure_columns = [bronze for bronze, target in pairs if is_measure(by_name[target])] + spread_columns

    # Roll-ups out first: a TOTALS row would otherwise be loaded as a partner.
    dropped = rollup_rows(frame, measure_columns)
    rows_in = frame.height
    if dropped:
        frame = frame.with_row_index("_row").filter(~pl.col("_row").is_in(dropped)).drop("_row")

    # One measure spread across columns: one row per cell.
    spread_measure = spread.measure if spread and spread.measure in by_name and spread_columns else None
    if spread_measure:
        dimension = spread.dimension if spread.dimension in by_name else None
        parts = []
        for name in spread_columns:
            parts.append(frame.drop([other for other in spread_columns if other != name]).rename({name: "_cell"})
                         .with_columns(pl.lit(headers.get(name) or name).alias("_header")))
        frame = pl.concat(parts, how="vertical_relaxed") if parts else frame
    height = frame.height

    columns: dict[str, pl.Series] = {}
    invalid: dict[str, int] = {}
    unreadable: dict[str, pl.Series] = {}

    def typed(name: str, series: pl.Series) -> None:
        column = by_name[name]
        value, failures = cleanse.by_type(series.alias(name), column)
        columns[name] = value
        if failures:
            invalid[name] = invalid.get(name, 0) + failures
            unreadable[name] = cleanse.text(series).is_not_null() & value.is_null()

    dimensions: list[tuple[str, pl.Series]] = []
    for bronze, target in pairs:
        if target in columns:
            continue
        typed(target, frame[bronze])
        if not is_measure(by_name[target]):
            dimensions.append((target, cleanse.text(frame[bronze])))
    if spread_measure:
        typed(spread_measure, frame["_cell"])
        if dimension and dimension not in columns:
            typed(dimension, frame["_header"])
        dimensions.append((dimension or spread.label, frame["_header"].cast(pl.String)))

    ids = frame["_ingestion_id"].to_list() if "_ingestion_id" in frame.columns else [None] * height
    loads = [context.loads.get(i) or LoadInfo() for i in ids]

    # The profit center: the load's, completed from the LOTL.
    resolved = [profit_center.resolve(None, None, load.pc_id or pc, lotl) for load in loads]
    statuses = [r[2] for r in resolved]

    # Each row's month: its sheet's; else the load's reporting dates.
    months = frame[MONTH].to_list() if MONTH in frame.columns else [None] * height
    starts, ends, kinds = [], [], []
    for month, load in zip(months, loads):
        if month:
            first = date(int(month[:4]), int(month[5:7]), 1)
            starts.append(first)
            ends.append(date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]))
            kinds.append("MONTH")
        else:
            starts.append(load.reporting_start_date)
            ends.append(load.reporting_end_date)
            kinds.append(load.reporting_period_type)

    slots = dimensions[:SLOTS]
    slot_values = [series.to_list() for _, series in slots]
    measure_names = [name for name in columns if is_measure(by_name[name])]
    measure_rows = pl.DataFrame({name: columns[name] for name in measure_names}).rows() if measure_names \
        else [()] * height
    periods = [_period(load.period_start, load.period_end) for load in loads]
    files = frame["_source_file"].to_list() if "_source_file" in frame.columns else [None] * height
    sheets = frame["_source_sheet"].to_list() if "_source_sheet" in frame.columns else [None] * height
    system: dict[str, list] = {
        "record_grain": [RECORD_GRAIN] * height,
        "report_type": [REPORT_TYPE] * height,
        "agg_data_source": [DATA_SOURCE] * height,
        "source_system": [context.source_system] * height,
        "source_table": [context.source_table] * height,
        "profit_center_name": [r[0] for r in resolved],
        "profit_center_number": [r[1] for r in resolved],
        "aggregation_category": [" x ".join(name.upper() for name, _ in slots) or None] * height,
        "reporting_start_date": starts,
        "reporting_end_date": ends,
        "reporting_year": [end.year if end else None for end in ends],
        "reporting_month": [end.month if end and kind == "MONTH" else None for end, kind in zip(ends, kinds)],
        "reporting_period": [f"{end:%Y-%m}" if end and kind == "MONTH" else None for end, kind in zip(ends, kinds)],
        "reporting_period_type": kinds,
        "accounting_effective_date": [start if kind == "MONTH" else None for start, kind in zip(starts, kinds)],
        "business_key_hash": [hashing.digest([context.source_system, r[1], f"{s}..{e}", *values])
                              for r, s, e, values in zip(resolved, starts, ends, zip(*slot_values) if slot_values
                                                         else [()] * height)],
        "row_hash": [hashing.digest(row) for row in measure_rows],
        "source_file": [load.file_name or name for load, name in zip(loads, files)],
        "source_sheet": sheets,
        "file_date": [load.file_received_date for load in loads],
        "ingestion_timestamp": [load.processing_date for load in loads],
        "processed_timestamp": [context.processed_at] * height,
        "silver_insert_timestamp": [context.processed_at] * height,
        "silver_update_timestamp": [context.processed_at] * height,
        "source_data_period_start_date": [p[0] for p in periods],
        "source_data_period_end_date": [p[1] for p in periods],
        "source_data_period_type": [p[2] for p in periods],
    }
    for number, (name, series) in enumerate(slots, 1):
        system[f"aggregation_dimension_{number}_name"] = [name] * height
        system[f"aggregation_dimension_{number}_value"] = series.to_list()

    ordered = []
    for column in catalog:
        if column.name == IDENTITY:
            continue
        if column.name in columns:
            ordered.append(columns[column.name].alias(column.name))
        elif column.name in system:
            ordered.append(_series(column, system[column.name]))
        else:
            ordered.append(_series(column, [None] * height))
    if extras:
        ordered += [pl.Series("_ingestion_id", ids, dtype=pl.String), pl.Series("_pc_status", statuses, dtype=pl.String),
                    _unreadable_names(unreadable, height)]
    result = pl.DataFrame(ordered) if ordered else pl.DataFrame()
    quality = {
        "rows": rows_in,
        "rows_loaded": height,
        "rollup_rows": len(dropped),
        "spread": {"columns": spread_columns, "measure": spread_measure,
                   "dimension": (spread.dimension or spread.label) if spread_measure else None},
        "invalid_values": invalid,
        "profit_center": dict(Counter(status for status in statuses if status)),
        "measures": {name: float(columns[name].cast(pl.Float64).sum() or 0) for name in measure_names},
        "dimensions": [name for name, _ in slots],
    }
    return result, quality
