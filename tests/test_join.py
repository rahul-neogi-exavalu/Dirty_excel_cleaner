"""Join key discovery and the margin rule that keeps fuzzy resolution honest."""

import pandas as pd
import pytest

from ahi_clean import join

PRODUCERS = pd.Series(
    [
        "MJC Agency Group",
        "Pinnacle Agency Partners",
        "Coastal Risk Advisors",
        "Apex Insurance Brokers",
        "Metro Agency Group",
    ]
)


def test_key_is_discovered_from_values_not_header_names(file6):
    fact, dimension = file6[0].frame, file6[1].frame
    key = join.discover_key(fact, dimension)
    assert key["fact_column"] == "producer_agency_name"
    assert key["dimension_column"] == "producer_agency_name"
    # The cheap exact pass settles it; fuzzy scoring never has to run.
    assert key["method"] == "exact_value_overlap"
    assert key["fuzzy_overlap"] is None


def test_truncated_value_auto_resolves_when_the_margin_is_wide():
    [resolution] = join.resolve_values(["MJC "], PRODUCERS)
    assert resolution["verdict"] == join.AUTO
    assert resolution["matched_value"] == "MJC Agency Group"
    assert resolution["best_score"] - resolution["runner_up_score"] >= join.AUTO_RESOLVE_MARGIN


def test_a_value_that_ties_two_candidates_is_flagged_not_guessed():
    """token_set_ratio scores 100 against every entry containing the tokens.

    The absolute threshold alone cannot see this; the margin rule can.
    """
    [resolution] = join.resolve_values(["Agency Group"], PRODUCERS)
    assert resolution["best_score"] >= join.AUTO_RESOLVE_SCORE
    assert resolution["verdict"] == join.AMBIGUOUS
    assert resolution["matched_value"] is None


def test_an_unrelated_value_is_left_unresolved():
    [resolution] = join.resolve_values(["Brown & Brown"], PRODUCERS)
    assert resolution["verdict"] == join.UNRESOLVED
    assert resolution["matched_value"] is None


def test_a_near_miss_is_routed_to_review_rather_than_merged():
    [resolution] = join.resolve_values(["Coastal Risk Advisers Inc"], PRODUCERS)
    assert resolution["verdict"] in {join.LOW_CONFIDENCE, join.AUTO}
    if resolution["verdict"] == join.LOW_CONFIDENCE:
        assert resolution["matched_value"] is None


def test_join_is_left_so_fact_rows_never_disappear(file6):
    fact, dimension = file6[0].frame, file6[1].frame
    # A producer the lookup has never heard of.
    fact = pd.concat([fact, fact.iloc[[0]].assign(producer_agency_name="Brown & Brown")], ignore_index=True)
    merged, report = join.join_frames(fact, dimension, join.discover_key(fact, dimension))

    assert len(merged) == len(fact)
    assert pd.isna(merged.iloc[-1]["state"])
    unresolved = [entry for entry in report["needs_review"] if entry["source_value"] == "Brown & Brown"]
    assert unresolved and unresolved[0]["verdict"] == join.UNRESOLVED


def test_dirty_key_is_resolved_and_enriches_every_row(file6):
    fact, dimension = file6[0].frame, file6[1].frame
    merged, report = join.join_frames(fact, dimension, join.discover_key(fact, dimension))

    assert report["needs_review"] == []
    assert merged["state"].notna().all()
    assert merged.loc[merged["producer_agency_name"] == "MJC", "state"].iloc[0] == "Texas"
    # The dimension's key column must not come back as a near-duplicate column.
    assert sum(column.startswith("producer") for column in merged.columns) == 1


def test_normalisation_collapses_whitespace_and_case():
    assert join.normalize_value("  MJC   Agency  Group ") == "mjc agency group"
    assert join.normalize_value(None) == ""


def test_low_cardinality_columns_are_never_treated_as_keys(file6):
    fact = file6[0].frame
    # insurance_company_name repeats heavily, so it cannot be a dimension key.
    dimension = pd.DataFrame({"carrier": ["Liberty Mutual"] * 5, "rating": list("ABCDE")})
    assert join.discover_key(fact, dimension) is None


def test_no_plausible_key_returns_none(file6):
    dimension = pd.DataFrame({"unrelated": ["alpha", "beta", "gamma"]})
    assert join.discover_key(file6[0].frame, dimension) is None
