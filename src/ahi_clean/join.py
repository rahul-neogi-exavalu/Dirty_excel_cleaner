"""Discover the join key between a fact sheet and a lookup sheet, and resolve it.

Header names are never used to find the key: ``producer_agency_name`` and
``producer`` do not string-match, and on a wider schema name matching happily
picks the wrong column. The key is found from the *values*.
"""

from __future__ import annotations

import re

import pandas as pd
from rapidfuzz import fuzz

EXACT_OVERLAP_THRESHOLD = 0.6
FUZZY_OVERLAP_THRESHOLD = 0.6
DISTINCT_RATIO_THRESHOLD = 0.9

AUTO_RESOLVE_SCORE = 90
# The winner must also beat the runner-up by this margin. token_set_ratio returns
# 100 whenever one side's tokens are a subset of the other's, so a generic or
# truncated value can clear the absolute threshold against several dimension
# entries at once -- 'Agency Group' scores 100 against both 'Metro Agency Group'
# and 'MJC Agency Group'. Without a margin the pipeline silently picks whichever
# max() saw first. With it, that value is flagged for a human instead.
AUTO_RESOLVE_MARGIN = 10
REVIEW_SCORE = 60

AUTO = "auto_resolved"
AMBIGUOUS = "ambiguous"
LOW_CONFIDENCE = "low_confidence"
UNRESOLVED = "unresolved"


def normalize_value(value) -> str:
    """Casefold, trim and collapse internal whitespace for key comparison."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _distinct_ratio(series: pd.Series) -> float:
    values = [normalize_value(value) for value in series if normalize_value(value)]
    return len(set(values)) / len(values) if values else 0.0


def _value_set(series: pd.Series) -> set[str]:
    return {normalize_value(value) for value in series if normalize_value(value)}


def discover_key(fact: pd.DataFrame, dimension: pd.DataFrame) -> dict | None:
    """Find the (fact column, dimension column) pair that looks like a join key.

    Two passes. The cheap one compares normalized value sets exactly and settles
    most cases outright -- on File6 it matches four of five producers and picks the
    pair immediately. Fuzzy scoring, which is quadratic in the number of values,
    only runs on pairs the cheap pass left below threshold, and only against
    dimension columns whose values are distinct enough to be a key at all.
    """
    candidates = []
    for dimension_column in dimension.columns:
        if _distinct_ratio(dimension[dimension_column]) < DISTINCT_RATIO_THRESHOLD:
            continue
        dimension_values = _value_set(dimension[dimension_column])
        if not dimension_values:
            continue
        for fact_column in fact.columns:
            fact_values = _value_set(fact[fact_column])
            if not fact_values:
                continue
            exact = len(fact_values & dimension_values) / len(fact_values)
            candidates.append(
                {
                    "fact_column": fact_column,
                    "dimension_column": dimension_column,
                    "exact_overlap": round(exact, 3),
                    "fuzzy_overlap": None,
                }
            )

    if not candidates:
        return None

    best = max(candidates, key=lambda candidate: candidate["exact_overlap"])
    if best["exact_overlap"] >= EXACT_OVERLAP_THRESHOLD:
        best["method"] = "exact_value_overlap"
        return best

    # Nothing matched cleanly -- pay for fuzzy scoring only now.
    for candidate in candidates:
        fact_values = _value_set(fact[candidate["fact_column"]])
        dimension_values = _value_set(dimension[candidate["dimension_column"]])
        matched = sum(
            1
            for value in fact_values
            if max((fuzz.token_set_ratio(value, other) for other in dimension_values), default=0)
            >= AUTO_RESOLVE_SCORE
        )
        candidate["fuzzy_overlap"] = round(matched / len(fact_values), 3)

    best = max(candidates, key=lambda candidate: candidate["fuzzy_overlap"] or 0.0)
    if (best["fuzzy_overlap"] or 0.0) >= FUZZY_OVERLAP_THRESHOLD:
        best["method"] = "fuzzy_value_overlap"
        return best
    return None


def resolve_values(fact_values, dimension_values) -> list[dict]:
    """Map each fact-side key to a dimension-side key, with a confidence verdict."""
    dimension_index = {normalize_value(value): value for value in dimension_values}
    resolutions: list[dict] = []

    for raw in fact_values:
        normalized = normalize_value(raw)
        if not normalized:
            continue
        if normalized in dimension_index:
            resolutions.append(
                {
                    "source_value": raw,
                    "matched_value": dimension_index[normalized],
                    "verdict": AUTO,
                    "best_score": 100.0,
                    "runner_up_score": None,
                    "method": "exact",
                }
            )
            continue

        scored = sorted(
            ((fuzz.token_set_ratio(normalized, key), original) for key, original in dimension_index.items()),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best_value = scored[0] if scored else (0.0, None)
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        margin = best_score - runner_up

        if best_score >= AUTO_RESOLVE_SCORE and margin >= AUTO_RESOLVE_MARGIN:
            verdict, matched = AUTO, best_value
        elif best_score >= AUTO_RESOLVE_SCORE:
            # Clears the threshold against more than one candidate: too close to call.
            verdict, matched = AMBIGUOUS, None
        elif best_score >= REVIEW_SCORE:
            verdict, matched = LOW_CONFIDENCE, None
        else:
            verdict, matched = UNRESOLVED, None

        resolutions.append(
            {
                "source_value": raw,
                "matched_value": matched,
                "candidate_value": best_value,
                "verdict": verdict,
                "best_score": round(float(best_score), 1),
                "runner_up_score": round(float(runner_up), 1),
                "method": "fuzzy_token_set",
            }
        )

    return resolutions


def join_frames(fact: pd.DataFrame, dimension: pd.DataFrame, key: dict) -> tuple[pd.DataFrame, dict]:
    """Left-join the dimension onto the fact table through a resolved key.

    Always a left join: a fact row must never disappear because its lookup missed.
    Unmatched rows keep their data and come back with null dimension columns, and
    the value is listed in the report for a human to look at.
    """
    fact_column = key["fact_column"]
    dimension_column = key["dimension_column"]

    resolutions = resolve_values(fact[fact_column].dropna().unique(), dimension[dimension_column].dropna())
    resolved_map = {
        entry["source_value"]: entry["matched_value"]
        for entry in resolutions
        if entry["verdict"] == AUTO and entry["matched_value"] is not None
    }

    working = fact.copy()
    resolved_key = "_resolved_join_key"
    working[resolved_key] = working[fact_column].map(lambda value: resolved_map.get(value))

    right = dimension.copy()
    # Drop the dimension's own copy of the key column so the join does not emit a
    # near-duplicate of the fact-side value under a suffixed name.
    right = right.rename(columns={dimension_column: resolved_key})
    overlapping = [column for column in right.columns if column in working.columns and column != resolved_key]
    right = right.drop(columns=overlapping)

    merged = working.merge(right, on=resolved_key, how="left").drop(columns=[resolved_key])

    report = {
        "join_key": {"fact_column": fact_column, "dimension_column": dimension_column},
        "discovery": {k: key[k] for k in ("exact_overlap", "fuzzy_overlap", "method") if k in key},
        "resolutions": resolutions,
        "needs_review": [entry for entry in resolutions if entry["verdict"] != AUTO],
        "fact_rows_in": len(fact),
        "fact_rows_out": len(merged),
        "enriched_rows": int(merged[right.columns.drop(resolved_key)[0]].notna().sum())
        if len(right.columns) > 1
        else 0,
    }
    return merged, report
