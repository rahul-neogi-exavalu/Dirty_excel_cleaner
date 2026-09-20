"""Join key discovery and the margin rule that keeps fuzzy resolution honest."""

import polars as pl

from ahi_clean import join

from conftest import HEADER, one, records

LOOKUP = [["Producer", "Address", "State", "ZIP Code"]] + [
    ["Pinnacle Agency Partners", "9515 Delegates Row", "IN", "46240"],
    ["Apex Insurance Brokers", "20 Village Center Dr", "NJ", "08085"],
    ["Metro Agency Group", "32646 Five Mile Rd", "MI", "48154"],
    ["Coastal Risk Advisors", "1514 Roberts Dr", "FL", "32250"],
]


def pair():
    """A fact table and its lookup, named differently on each side."""
    return one([HEADER] + records(8)).frame, one(LOOKUP, name="Lookup").frame

PRODUCERS = pl.Series(
    [
        "MJC Agency Group",
        "Pinnacle Agency Partners",
        "Coastal Risk Advisors",
        "Apex Insurance Brokers",
        "Metro Agency Group",
    ]
)


def test_key_is_discovered_from_values_not_header_names():
    fact, dimension = pair()
    key = join.discover_key(fact, dimension)
    # The two columns are named differently in their own files -- 'Producer/AgencyName'
    # and 'Producer' -- and nothing renames them. The pair is found from the values.
    assert key["fact_column"] == "producer_agencyname"
    assert key["dimension_column"] == "producer"
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


def test_join_is_left_so_fact_rows_never_disappear():
    fact, dimension = pair()
    key = join.discover_key(fact, dimension)
    # A producer the lookup has never heard of, added after the key is known -- as it
    # would be in a later file using the same schema.
    stray = fact.head(1).with_columns(pl.lit("Brown & Brown").alias("producer_agencyname"))
    fact = pl.concat([fact, stray], how="vertical_relaxed")
    merged, report = join.join_frames(fact, dimension, key)

    assert len(merged) == len(fact)
    assert merged["state"][-1] is None
    unresolved = [entry for entry in report["needs_review"] if entry["source_value"] == "Brown & Brown"]
    assert unresolved and unresolved[0]["verdict"] == join.UNRESOLVED


def test_the_dimension_key_does_not_return_as_a_duplicate_column():
    fact, dimension = pair()
    merged, _report = join.join_frames(fact, dimension, join.discover_key(fact, dimension))
    assert sum(column.startswith("producer") for column in merged.columns) == 1


def test_normalisation_collapses_whitespace_and_case():
    assert join.normalize_value("  MJC   Agency  Group ") == "mjc agency group"
    assert join.normalize_value(None) == ""


def test_low_cardinality_columns_are_never_treated_as_keys():
    fact, _dimension = pair()
    # insurance_company_name repeats heavily, so it cannot be a dimension key.
    dimension = pl.DataFrame({"carrier": ["Liberty Mutual"] * 5, "rating": list("ABCDE")})
    assert join.discover_key(fact, dimension) is None


def test_no_plausible_key_returns_none():
    dimension = pl.DataFrame({"unrelated": ["alpha", "beta", "gamma"]})
    fact, _dimension = pair()
    assert join.discover_key(fact, dimension) is None
