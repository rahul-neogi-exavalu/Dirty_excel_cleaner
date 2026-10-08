"""Two bronze tables of files that came together, joined into one table for Silver.

A profit center may send a list beside its data -- a broker list beside the transactions,
received together. In Bronze each file is its own table (a ``file_name`` column says which
file a row came from); in Silver the two are joined, on keys the reviewer confirms, so the
transaction rows carry the list's columns:

* only the rows of files that came together are matched (``pairs``: each data load and
  the list load that came with it), never another month's list;
* keys are compared trimmed, with runs of spaces as one, and -- unless the reviewer says
  otherwise -- without case;
* ``left`` keeps every data row (unmatched ones get empty list columns); ``inner`` keeps
  the matched rows only. (A right join is the left join with the tables swapped: the
  caller swaps them, so every row still has a data load behind it.)
* a key repeated in the list would repeat the data rows it matches -- and count their
  premium twice -- so repeats are reported for the reviewer to resolve, never joined
  silently.

The list's columns come back prefixed with its table name (``ext_pc0101_brokers.region``).
"""

from __future__ import annotations

import polars as pl

LEFT, INNER, RIGHT = "left", "inner", "right"
HOWS = (LEFT, RIGHT, INNER)
PAIR = "_pair"


def key_expr(column: str, ignore_case: bool) -> pl.Expr:
    """The key as compared: trimmed, spaces collapsed, case folded (``ignore_case``)."""
    text = pl.col(column).cast(pl.String).str.strip_chars().str.replace_all(r"\s+", " ")
    text = text.str.to_lowercase() if ignore_case else text
    return pl.when(text == "").then(None).otherwise(text)


def prefixed(table: str, column: str) -> str:
    return f"{table}.{column}"


def join_frames(left: pl.DataFrame, right: pl.DataFrame, *, right_table: str, keys: list[tuple[str, str]],
                how: str, ignore_case: bool, pairs: list[tuple[str, str]], lineage: list[str]) -> tuple[pl.DataFrame, dict]:
    """``left``: the data rows (with ``lineage``); ``right``: the list's rows (with
    ``_ingestion_id``). ``keys``: (left column, right column). ``pairs``: (left load,
    right load) that came together. Returns the joined rows -- left's columns, then the
    right's prefixed -- and what the join did: rows matched, left unmatched, and the
    right's repeated keys."""
    if how not in (LEFT, INNER):
        raise ValueError(f"join how must be left or inner here, not {how}")
    left_pair = {own: str(index) for index, (own, _) in enumerate(pairs)}
    right_pair = {other: str(index) for index, (_, other) in enumerate(pairs)}
    lk = [f"_lk{index}" for index in range(len(keys))]
    rk = [f"_rk{index}" for index in range(len(keys))]
    data_right = [name for name in right.columns if name not in lineage and name != "_ingestion_id"]

    a = left.with_columns(
        pl.col("_ingestion_id").replace_strict(left_pair, default=None, return_dtype=pl.String).alias(PAIR),
        *(key_expr(own, ignore_case).alias(name) for (own, _), name in zip(keys, lk)))
    b = right.with_columns(
        pl.col("_ingestion_id").replace_strict(right_pair, default=None, return_dtype=pl.String).alias(PAIR),
        *(key_expr(other, ignore_case).alias(name) for (_, other), name in zip(keys, rk)))
    b = b.filter(pl.col(PAIR).is_not_null()).select(
        [PAIR, *rk, *(pl.col(name).alias(prefixed(right_table, name)) for name in data_right)])

    # Repeated keys in the list: each would repeat the data rows it matches.
    repeats = (b.drop_nulls(rk).group_by([PAIR, *rk]).len().filter(pl.col("len") > 1)
               .sort("len", descending=True))
    duplicates = [{"key": " | ".join(str(row[name]) for name in rk), "rows": int(row["len"])}
                  for row in repeats.head(20).iter_rows(named=True)]

    joined = a.join(b, left_on=[PAIR, *lk], right_on=[PAIR, *rk], how=how, coalesce=True, nulls_equal=False)
    matched_flag = pl.any_horizontal([pl.col(prefixed(right_table, name)).is_not_null() for name in data_right]) \
        if data_right else pl.lit(False)
    stats = {
        "rows": left.height,
        "joined": joined.height,
        "matched": int(joined.select(matched_flag.sum()).item()) if joined.height else 0,
        "unmatched": int(joined.select((~matched_flag).sum()).item()) if how == LEFT and joined.height else
        left.height - joined.height,
        "duplicates": duplicates,
        "repeated_keys": int(repeats.height),
    }
    return joined.drop([PAIR, *lk, *[name for name in rk if name in joined.columns]]), stats
