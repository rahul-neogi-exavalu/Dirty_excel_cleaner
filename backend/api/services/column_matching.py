"""Which source column is which Silver column: the helpers both column reviews share.

The Silver review maps every bronze column to the catalog; the Validate step (before
Bronze) maps a cleaned file's columns to the required ones. Both let the same methods vote
(``ahi_silver.matching``: saved, exact, fuzzy, word2vec, the AI) and read saved decisions
the same way, so a mapping approved in one is a vote in the other.
"""

from __future__ import annotations

from collections import Counter

import polars as pl

from ahi_bronze import file_meta
from ahi_silver import normalize, semantic

from .. import config


def header_key(text: str | None) -> str:
    """A header as written, without case or extra spaces: 'Net  Premium\\n' -> 'net premium'."""
    return " ".join(str(text or "").split()).casefold()


def same_pc(left: str | None, right: str | None) -> bool:
    return bool(left and right) and file_meta.normalize_pc_id(left) == file_meta.normalize_pc_id(right)


def saved_votes(rows: list[tuple], pcs: list[str], columns: list[str],
           headers: dict[str, str] | None = None) -> tuple[dict, dict[str, str], set[str]]:
    """The table's profit centers' approved mapping for these bronze columns, and approved
    names elsewhere (by normalized name).

    The DRT mapping names a source column as the file wrote it ("Net Premium"). A bronze
    column meets it by its own source header, exactly (spaces and case aside); a column
    whose header is unknown (loaded before headers were kept) meets it on normalized words
    ("net_premium"). Returns ({column: [silver, ...]}, {normalized name: silver}, and the
    columns whose several targets are one header mapped twice -- a real one-to-many).
    Two different headers that normalize alike ("Agent Commission" and "Agent
    Commission%") are not one-to-many: their targets compete as votes. A row naming a DRT
    label with no Silver column is not a decision and is skipped.
    """
    headers = headers or {}
    by_header: dict[str, str] = {}
    for column in columns:
        by_header.setdefault(header_key(headers.get(column) or column), column)
    by_compact = {normalize.compact(column): column for column in columns}
    own: dict[str, list[str]] = {}
    seen_headers: dict[str, set[str]] = {}
    votes: dict[str, Counter] = {}
    for profit_center_code, pc_column, _drt_column, silver in rows:
        key = normalize.compact(pc_column or "")
        if not key or not silver:
            continue
        if any(same_pc(profit_center_code, pc) for pc in pcs):
            column = by_header.get(header_key(pc_column)) or by_compact.get(key)
            if column is None:
                continue
            targets = own.get(column) or []
            if silver not in targets:
                own[column] = targets + [silver]
            seen_headers.setdefault(column, set()).add(header_key(pc_column))
        else:
            votes.setdefault(key, Counter())[silver] += 1
    known = {name: counter.most_common(1)[0][0] for name, counter in votes.items()}
    one_to_many = {column for column, targets in own.items()
                   if len(targets) > 1 and len(seen_headers.get(column, ())) == 1}
    return own, known, one_to_many


# Notes the business keeps in the mapping's source-column field ("check comments",
# "08/05: Sara suggested ...") are not column names: never shown to the AI as examples.
_NOT_A_COLUMN = ("comment", "disregard", "sara", "check ", "suggested", "backfill", "(tab", "data profile")


def precedents(rows: list[tuple], pc: str | None = None) -> list[tuple[str, str]]:
    """Approved decisions worth showing the AI as examples, this profit center's first:
    a renamed or abbreviated column (Bk MGA Comm -> gross_commission_amount). A column
    named exactly like its Silver column teaches nothing."""
    picked: dict[tuple[str, str], bool] = {}
    for profit_center_code, pc_column, _drt_column, silver in rows:
        header = " ".join((pc_column or "").split())
        if not header or len(header) > 40 or any(word in header.casefold() for word in _NOT_A_COLUMN):
            continue
        if silver is None:
            continue  # a DRT label without a Silver column: not a decision
        if normalize.compact(header) == normalize.compact(silver):
            continue
        pair = (header, silver)
        picked[pair] = picked.get(pair, False) or same_pc(profit_center_code, pc)
    return sorted(picked, key=lambda pair: (not picked[pair], pair[0].casefold(), pair[1]))


def samples(frame: pl.DataFrame, columns: list[str]) -> dict[str, list[str]]:
    result = {}
    for column in columns:
        if column in frame.columns:
            values = [value for value in frame[column].drop_nulls().unique(maintain_order=True).to_list() if str(value).strip()]
            result[column] = [str(value) for value in values[:3]]
    return result


def matchers(precedents=(), vectors_progress: semantic.Progress | None = None):
    """The word2vec similarity and the AI matcher (each None when not configured), and notes.
    ``vectors_progress`` hears how far reading the word2vec file has got (first use only)."""
    similarity = None
    notes = []
    if config.WORD2VEC_PATH:
        try:
            similarity = semantic.load(config.WORD2VEC_PATH, progress=vectors_progress).similarity
        except OSError as error:
            notes.append(f"word2vec vectors could not be read: {error}")
    llm = None
    if config.AI_ENABLED and config.AI_PROVIDER == "azure_openai":
        from ahi_silver.llm import azure_openai_matcher

        llm = azure_openai_matcher(config.AZURE_OPENAI_API_KEY, config.AZURE_OPENAI_ENDPOINT,
                                   config.AZURE_OPENAI_API_VERSION, config.AZURE_OPENAI_DEPLOYMENT,
                                   config.AI_SEND_SAMPLES, precedents)
    elif config.AI_ENABLED and config.AI_PROVIDER == "gemini":
        from ahi_silver.llm import gemini_matcher

        llm = gemini_matcher(config.GEMINI_API_KEY, config.GEMINI_MODEL, config.AI_SEND_SAMPLES, precedents)
    return similarity, llm, notes
