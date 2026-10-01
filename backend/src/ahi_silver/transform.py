"""One bronze table's rows -> Silver rows, under an approved mapping.

Used twice with the same code: for the dry-run quality report the reviewer sees before
approving, and for the load itself -- so what was reviewed is what is written.
"""

from __future__ import annotations

from collections import Counter

import polars as pl

from . import cleanse, hashing, profit_center
from .catalog import SilverColumn

NAME, NUMBER = "profit_center_name", "profit_center_number"
LINEAGE = ["_ingestion_id", "_source_file", "_source_sheet"]


def transform(
    frame: pl.DataFrame,
    mapping: dict[str, str],
    catalog: list[SilverColumn],
    pc: str | None,
    lotl: profit_center.Lotl,
) -> tuple[pl.DataFrame, dict]:
    """``frame``: bronze rows as text. ``mapping``: bronze column -> Silver column.

    Returns the Silver frame (catalog columns, typed, then pc_id, pc_lookup_status,
    business_key_hash, row_hash and lineage) and a quality report.
    """
    source_for = {silver: bronze for bronze, silver in mapping.items() if silver}
    height = frame.height
    columns: dict[str, pl.Series] = {}
    invalid: dict[str, int] = {}
    for column in catalog:
        bronze = source_for.get(column.name)
        series = frame[bronze] if bronze in frame.columns else pl.Series(column.name, [None] * height, dtype=pl.String)
        typed, failures = cleanse.by_type(series.alias(column.name), column.data_type)
        columns[column.name] = typed
        if failures:
            invalid[column.name] = failures

    # Profit center: the document's four scenarios, row by row.
    statuses: list[str] = []
    if NAME in columns or NUMBER in columns:
        names = columns[NAME].to_list() if NAME in columns else [None] * height
        numbers = columns[NUMBER].to_list() if NUMBER in columns else [None] * height
        resolved = [profit_center.resolve(n, m, pc, lotl) for n, m in zip(names, numbers)]
        if NAME in columns:
            columns[NAME] = pl.Series(NAME, [r[0] for r in resolved], dtype=pl.String)
        if NUMBER in columns:
            columns[NUMBER] = pl.Series(NUMBER, [r[1] for r in resolved], dtype=pl.String)
        statuses = [r[2] for r in resolved]
    else:
        statuses = [None] * height

    silver = pl.DataFrame(columns)
    names = [column.name for column in catalog]
    keys = [column.name for column in catalog if column.business_key]
    rows = silver.select(names).rows()
    key_index = [names.index(name) for name in keys]
    business = [hashing.digest([row[i] for i in key_index]) if keys else None for row in rows]
    whole = [hashing.digest(row) for row in rows]

    extra = {
        "pc_id": pl.Series("pc_id", [pc] * height, dtype=pl.String),
        "pc_lookup_status": pl.Series("pc_lookup_status", statuses, dtype=pl.String),
        "business_key_hash": pl.Series("business_key_hash", business, dtype=pl.String),
        "row_hash": pl.Series("row_hash", whole, dtype=pl.String),
    }
    for name in LINEAGE:
        extra[name] = frame[name] if name in frame.columns else pl.Series(name, [None] * height, dtype=pl.String)
    silver = silver.with_columns(list(extra.values()))

    quality = {
        "rows": height,
        "invalid_values": invalid,
        "profit_center": dict(Counter(status for status in statuses if status)),
        "unmapped_silver_columns": [name for name in names if name not in source_for],
    }
    return silver, quality
