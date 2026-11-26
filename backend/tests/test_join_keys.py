"""Join keys between two tables that came together, suggested by votes (no database)."""

from ahi_silver import matching

TRANSACTIONS = {
    "policy_number": ["POL-1", "POL-2", "POL-3", "POL-4", "POL-5"],
    "producer_name": ["Brown & Brown", "acme", "Lockton ", "Marsh", "Brown & Brown"],
    "premium": ["100", "250", "75", "310", "90"],
}
BROKERS = {
    "account_name": ["Brown & Brown", "Acme", "Lockton", "Marsh", "Aon", "Gallagher"],
    "market_provider": ["Market A", "Market B", "Market A", "Market C", "Market B", "Market C"],
    "region": ["East", "West", "North", "South", "East", "West"],
}


def test_the_values_find_a_key_whose_names_disagree():
    pairs, notes = matching.suggest_keys(TRANSACTIONS, BROKERS)
    best = pairs[0]
    assert (best.left, best.right) == ("producer_name", "account_name")
    assert matching.OVERLAP in best.methods and best.overlap == 1.0  # case and spaces aside
    assert best.unique == 1.0
    assert all(pair.right != "account_name" for pair in pairs[1:])  # a right column is suggested once


def test_a_saved_join_and_the_same_name_vote_too():
    left = {"broker": ["A", "B"], "premium": ["1", "2"]}
    right = {"broker": ["A", "B", "C"], "notes": ["x", "y", "z"]}
    pairs, _ = matching.suggest_keys(left, right, saved=[("broker", "broker")])
    assert (pairs[0].left, pairs[0].right) == ("broker", "broker")
    assert {matching.SAVED, matching.EXACT, matching.OVERLAP} <= set(pairs[0].methods)


def test_keys_are_compared_trimmed_and_without_case():
    assert matching.key_value("  Brown   &  Brown ") == "brown & brown"
    assert matching.key_value("ACME", ignore_case=False) == "ACME" and matching.key_value("  ") is None


# --- the join itself ------------------------------------------------------------------

import polars as pl  # noqa: E402

from ahi_silver import join  # noqa: E402

LINEAGE = ["_ingestion_id", "_source_file", "_source_sheet"]


def _txn(load="txn-aug", names=("Brown & Brown", "acme", "Lockton ", "Nobody")):
    return pl.DataFrame({"producer_name": list(names), "premium": ["100", "250", "75", "5"],
                         "_ingestion_id": [load] * len(names), "_source_file": ["t.xlsx"] * len(names),
                         "_source_sheet": ["Data"] * len(names)})


def _brokers(load="brk-aug", names=("Brown & Brown", "Acme", "Lockton")):
    return pl.DataFrame({"account_name": list(names), "region": ["East", "West", "North"][: len(names)],
                         "_ingestion_id": [load] * len(names)})


def _join(left, right, how="left", pairs=(("txn-aug", "brk-aug"),)):
    return join.join_frames(left, right, right_table="ext_pc0101_brokers", keys=[("producer_name", "account_name")],
                            how=how, ignore_case=True, pairs=list(pairs), lineage=LINEAGE)


def test_a_left_join_keeps_every_data_row_and_brings_the_lists_columns():
    rows, stats = _join(_txn(), _brokers())
    assert rows.columns == ["producer_name", "premium", *LINEAGE, "ext_pc0101_brokers.account_name",
                            "ext_pc0101_brokers.region"]
    assert rows["ext_pc0101_brokers.region"].to_list() == ["East", "West", "North", None]
    assert (stats["rows"], stats["joined"], stats["matched"], stats["unmatched"]) == (4, 4, 3, 1)
    assert stats["duplicates"] == []


def test_an_inner_join_keeps_only_the_matched_rows():
    rows, stats = _join(_txn(), _brokers(), how="inner")
    assert rows.height == 3 and stats["unmatched"] == 1 and "Nobody" not in rows["producer_name"].to_list()


def test_only_files_that_came_together_are_matched():
    # July's broker list never answers for August's transactions.
    rows, stats = _join(_txn(), _brokers(load="brk-jul"), pairs=[("txn-aug", "brk-aug"), ("txn-jul", "brk-jul")])
    assert stats["matched"] == 0 and rows["ext_pc0101_brokers.region"].null_count() == 4


def test_a_key_repeated_in_the_list_is_reported_not_joined_silently():
    _, stats = _join(_txn(), _brokers(names=("Brown & Brown", "brown &  brown", "Acme")))
    assert stats["repeated_keys"] == 1 and stats["duplicates"] == [{"key": "brown & brown", "rows": 2}]
