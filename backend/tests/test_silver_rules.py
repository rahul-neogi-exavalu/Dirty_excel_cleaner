"""The AHI Bronze -> Silver document, rule by rule, without a database.

Behaviour tests: everything is built in memory. word2vec and Gemini are replaced by
small stand-ins, so the voting logic is pinned without a model or an API key.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from ahi_silver import catalog, cleanse, hashing, llm, matching, normalize, profit_center, semantic
from ahi_silver.catalog import SilverColumn

CATALOG = catalog.load(Path(__file__).resolve().parents[1] / "config" / "silver_columns.csv")
NAMES = {column.name for column in CATALOG}


def run(columns, saved=None, known=None, **kwargs):
    suggestions, notes = matching.suggest(columns, CATALOG, saved or {}, known or {}, **kwargs)
    return {s.bronze_column: s for s in suggestions}, notes


# --- normalisation ----------------------------------------------------------------


@pytest.mark.parametrize("name, words", [
    ("profitcentername", "profit center name"),
    ("acct_eff_date", "accounting effective date"),
    ("pc_no", "profit center number"),
    ("Premium Amt", "premium amount"),
])
def test_names_become_words(name, words):
    assert normalize.phrase(name) == words


def test_the_draft_catalog_loads():
    assert {"premium", "profit_center_number", "accounting_effective_date"} <= NAMES
    assert any(column.business_key for column in CATALOG)


# --- matching: every method votes --------------------------------------------------


def methods(suggestion):
    """The methods that voted for the column currently chosen."""
    return [vote.method for vote in suggestion.backers()]


def by_target(suggestion):
    return {candidate.silver_column: candidate for candidate in suggestion.candidates}


def test_saved_mapping_is_reused_including_an_ignore():
    found, _ = run(["premium_amt", "notes"], saved={"premium_amt": "premium", "notes": None})
    assert found["premium_amt"].silver_column == "premium" and matching.SAVED in methods(found["premium_amt"])
    assert found["notes"].ignored and found["notes"].decided
    assert found["notes"].selection == matching.RECOMMENDED


def test_a_name_approved_for_another_table_is_suggested():
    found, _ = run(["carrier"], known={normalize.compact("carrier"): "insurance_company_name"})
    assert found["carrier"].silver_column == "insurance_company_name"
    assert methods(found["carrier"]) == [matching.SAVED]


def test_exact_matches_the_silver_or_drt_name():
    found, _ = run(["accountingeffectivedate", "Profit Center Number", "pol_eff_date"])
    assert all(matching.EXACT in methods(s) for s in found.values())
    assert found["pol_eff_date"].silver_column == "policy_effective_date"


def test_fuzzy_needs_a_shared_distinctive_word():
    found, _ = run(["premum", "carrier_name"])
    assert found["premum"].silver_column == "premium" and methods(found["premum"]) == [matching.FUZZY]
    # "carrier name" shares only "name" with any target: no fuzzy vote.
    assert found["carrier_name"].silver_column is None and not found["carrier_name"].candidates


def test_a_fuzzy_tie_is_no_vote():
    # "policy" is as close to policy_number as to policy_effective_date.
    found, _ = run(["policy"])
    assert found["policy"].silver_column is None


def test_semantic_votes_with_word_vectors(tmp_path):
    vectors = tmp_path / "tiny.txt"
    rows = {"carrier": [1, 0.1, 0], "insurer": [1, 0.15, 0], "insurance": [0.95, 0.2, 0],
            "company": [0.9, 0.25, 0], "agency": [0, 1, 0], "producer": [0.05, 1, 0],
            "agent": [0, 0.95, 0.1], "name": [0.3, 0.3, 1]}
    vectors.write_text("8 3\n" + "\n".join(f"{w} {' '.join(map(str, v))}" for w, v in rows.items()), encoding="utf-8")
    table = semantic.load(str(vectors))
    found, notes = run(["carrier", "agency"], similarity=table.similarity, semantic_min=0.9)
    assert found["carrier"].silver_column == "insurance_company_name"
    assert found["agency"].silver_column == "producer_name"
    assert methods(found["carrier"]) == methods(found["agency"]) == [matching.SEMANTIC]


def test_binary_word2vec_files_load(tmp_path):
    path = tmp_path / "tiny.bin"
    with open(path, "wb") as handle:
        handle.write(b"2 3\n")
        for word, vector in (("premium", [1, 0, 0]), ("amount", [0, 1, 0])):
            handle.write(word.encode() + b" " + np.asarray(vector, dtype=np.float32).tobytes() + b"\n")
    table = semantic.load(str(path))
    assert table.similarity(("premium",), ("premium",)) == pytest.approx(1.0)


def test_ai_answers_are_checked_against_the_catalog():
    def stub(columns, targets):
        return {"writing_agency": ("producer_name", 0.9, "agency that wrote it"),
                "notes": ("made_up_column", 0.99, "invented")}
    found, _ = run(["writing_agency", "notes"], llm=stub)
    assert found["writing_agency"].silver_column == "producer_name"
    assert methods(found["writing_agency"]) == [matching.AI]
    assert found["notes"].silver_column is None and not found["notes"].candidates  # invented: dropped


def test_ai_failure_does_not_stop_the_review():
    def broken(columns, targets):
        raise RuntimeError("quota exceeded")
    found, notes = run(["writing_agency"], llm=broken)
    assert found["writing_agency"].selection == matching.NONE
    assert any("AI unavailable" in note for note in notes)


def test_missing_methods_are_reported():
    _, notes = run(["mystery"])
    assert any("word2vec" in note for note in notes) and any("AI matching skipped" in note for note in notes)


def test_undecided_and_duplicate_targets_block_approval():
    suggestions, _ = matching.suggest(["premium", "premium_amt", "mystery"], CATALOG, {}, {})
    matching.choose(suggestions[1], "premium", False)
    issues = matching.problems("t", suggestions)
    assert any("mystery" in issue for issue in issues)
    assert any("all map to premium" in issue for issue in issues)


# The user's example: fuzzy reads one column, word2vec and Gemini read another. Both are
# offered; the one more methods agree on is pre-selected.
PREMIUMS = [
    SilverColumn("total_premium", "Total Premium", "decimal", False, "Total premium for the policy (gross premium)"),
    SilverColumn("premium", "Premium", "decimal", False, "Base premium before adjustments"),
]


def test_every_method_votes_and_the_best_supported_candidate_is_recommended():
    def similarity(left, right):
        return 0.91 if "total" in right else 0.4

    def gemini(columns, targets):
        return {"prem_amt": ("total_premium", 0.85, "prem amt -> premium amount; the policy total")}

    suggestions, _ = matching.suggest(["prem_amt"], PREMIUMS, {}, {}, similarity=similarity, llm=gemini)
    found = suggestions[0]
    candidates = by_target(found)
    assert candidates["total_premium"].methods == [matching.SEMANTIC, matching.AI]
    assert candidates["premium"].methods == [matching.FUZZY]
    assert found.silver_column == "total_premium" and candidates["total_premium"].recommended
    assert found.split and found.candidates[0].silver_column == "total_premium"
    assert "premium (Fuzzy)" in found.reason and "Recommended by Semantic + AI" in found.reason


def test_methods_run_independently_of_each_other():
    asked = []

    def gemini(columns, targets):
        asked.extend(columns)
        return {"premium": ("premium", 0.95, "same name")}

    found, _ = run(["premium", "premium_amt"], saved={"premium_amt": "premium"}, llm=gemini)
    # Exact matched "premium" and the saved mapping covered "premium_amt": Gemini is
    # still asked about both, and fuzzy still votes on both.
    assert asked == ["premium", "premium_amt"]
    assert matching.FUZZY in by_target(found["premium_amt"])["premium"].methods
    assert by_target(found["premium"])["premium"].methods == [matching.EXACT, matching.FUZZY, matching.AI]


def test_a_saved_decision_outranks_more_votes():
    found, _ = run(["premium"], saved={"premium": "commission"})
    assert found["premium"].silver_column == "commission"  # the reviewer decided this earlier
    assert found["premium"].split and by_target(found["premium"])["premium"].support == 2


def test_two_columns_wanting_one_target_the_better_supported_keeps_it():
    found, _ = run(["premium", "premium_amt"])
    assert found["premium"].silver_column == "premium"
    assert found["premium_amt"].silver_column is None  # its only candidate is taken
    assert "premium" in by_target(found["premium_amt"])  # still offered in the dropdown
    assert "recommended for premium" in found["premium_amt"].reason


def test_ai_saying_nothing_fits_is_a_vote_not_an_ignore():
    def gemini(columns, targets):
        return {"pol_exp_dt": (None, 0.9, "policy expiration date; no expiration target")}

    found, _ = run(["pol_exp_dt"], llm=gemini)
    suggestion = found["pol_exp_dt"]
    assert not suggestion.decided and suggestion.reason.startswith("AI:")
    assert by_target(suggestion)[None].methods == [matching.AI]  # offered as Ignore


def test_ai_second_choice_is_offered_but_not_counted():
    def gemini(columns, targets):
        return {"prod_nm": ("producer_name", 0.6, "prod -> producer or product", "line_of_business")}

    found, _ = run(["prod_nm"], llm=gemini)
    second = by_target(found["prod_nm"])["line_of_business"]
    assert second.support == 0 and second.votes[0].second_choice
    assert found["prod_nm"].silver_column == "producer_name" and not found["prod_nm"].split


def test_picking_the_recommendation_again_restores_it():
    found, _ = run(["premium"])
    suggestion = found["premium"]
    matching.choose(suggestion, "commission", False)
    assert suggestion.selection == matching.MANUAL and suggestion.backers() == []
    matching.choose(suggestion, "premium", False)
    assert suggestion.selection == matching.RECOMMENDED


# --- the Gemini prompt (no network) ---------------------------------------------------


def _examples(prompt):
    block = prompt.split("<examples>\n")[1].split("\n</examples>")[0]
    return {e["bronze_column"]: e for e in map(json.loads, block.splitlines())}


def test_prompt_teaches_abbreviations_with_contrast_examples():
    prompt = llm.build_prompt({"eff_dt": [], "acc_eff_dt": []}, CATALOG)
    examples = _examples(prompt)
    # Same meaning, same answer and confidence.
    assert (examples["eff_dt"]["silver_column"], examples["eff_dt"]["confidence"]) == \
        (examples["effective_date"]["silver_column"], examples["effective_date"]["confidence"]) == \
        ("policy_effective_date", 0.7)
    assert examples["acc_eff_dt"]["silver_column"] == examples["accountingeffectivedate"]["silver_column"] == \
        "accounting_effective_date"
    # Contrast: a code is not a name, the insured is not the insurer.
    assert examples["carrier_cd"]["silver_column"] is None and examples["insd_nm"]["silver_column"] is None
    assert "<source_columns>\n- eff_dt\n- acc_eff_dt\n</source_columns>" in prompt


def test_prompt_drops_examples_for_columns_not_in_the_list():
    small = [column for column in CATALOG if column.name != "transaction_effective_date"]
    prompt = llm.build_prompt({"x": []}, small)
    assert "trn_eff_dte" not in prompt and "transaction_effective_date" not in prompt


def test_approved_mappings_become_precedents_but_never_for_the_asked_column():
    approved = [("acc_pst_dt", "accounting_effective_date"), ("upload_ts", None),
                ("eff_dt", "policy_effective_date"), ("old", "column_that_was_dropped")]
    prompt = llm.build_prompt({"eff_dt": []}, CATALOG, approved)
    block = prompt.split("<approved>\n")[1].split("\n</approved>")[0]
    assert '"acc_pst_dt"' in block and '"upload_ts"' in block
    assert '"eff_dt"' not in block and "column_that_was_dropped" not in block


def test_samples_are_sent_only_when_allowed():
    assert "Toledo" not in llm.build_prompt({"office": ["Toledo"]}, CATALOG)
    assert '"Toledo"' in llm.build_prompt({"office": ["Toledo"]}, CATALOG, send_samples=True)


# --- cleansing --------------------------------------------------------------------


def test_every_date_format_in_the_document():
    values = ["2026-01-15 10:30:00", "2026-01-16", "01/17/2026", "1/8/2026", "20260119",
              "01-20-2026", "1-9-2026", "46030", "not a date", "13/45/2026", None, "  "]
    parsed, invalid = cleanse.dates(pl.Series("d", values))
    assert parsed.to_list()[:8] == [date(2026, 1, 15), date(2026, 1, 16), date(2026, 1, 17), date(2026, 1, 8),
                                    date(2026, 1, 19), date(2026, 1, 20), date(2026, 1, 9), date(2026, 1, 8)]
    assert parsed.to_list()[8:] == [None, None, None, None]
    assert invalid == 2  # blanks are not failures


def test_every_decimal_format_in_the_document():
    parsed, invalid = cleanse.decimals(pl.Series("p", ["1200.50", "1,200.50", "$1,200.50", "(250.00)", "abc", None]))
    assert parsed.to_list() == [Decimal("1200.50")] * 3 + [Decimal("-250.00"), None, None]
    assert invalid == 1


def test_strings_are_trimmed_and_blanks_become_null():
    assert cleanse.text(pl.Series("s", ["  Toledo  ", "", "   ", None])).to_list() == ["Toledo", None, None, None]


# --- profit center ----------------------------------------------------------------

LOTL = profit_center.Lotl.from_rows([("0094", "Dayton Office", "94"), ("0515", "Toledo Office", "515"),
                                     ("0303", "Columbus Office", "0303")])


@pytest.mark.parametrize("name, number, expected", [
    ("Dayton Office", 94, ("Dayton Office", "0094", profit_center.KEPT)),
    ("Toledo Office", None, ("Toledo Office", "0515", profit_center.FILLED_NUMBER)),
    ("Dayton Office", 515, ("Dayton Office", "0094", profit_center.CORRECTED)),
    (None, None, ("Columbus Office", "0303", profit_center.FILLED_NAME)),
    ("Unknown Office", 777, ("Unknown Office", "0777", profit_center.NO_MATCH)),
])
def test_the_four_profit_center_scenarios(name, number, expected):
    assert profit_center.resolve(name, number, "0303", LOTL) == expected


def test_without_a_lotl_rows_are_kept_and_flagged():
    assert profit_center.resolve("Dayton Office", 94, "0303", profit_center.Lotl()) == (
        "Dayton Office", "0094", profit_center.UNAVAILABLE)


@pytest.mark.parametrize("value, padded", [(94, "0094"), ("515", "0515"), ("94.0", "0094"), ("PC0001", "PC0001"), ("", None)])
def test_numbers_are_four_digits(value, padded):
    assert profit_center.pad(value) == padded


def test_pc_id_is_the_numeric_part_of_the_source_system():
    assert profit_center.pc_id("pc0515") == "0515"


# --- hashes -----------------------------------------------------------------------


def test_hashes_are_stable_and_normalised():
    a = hashing.digest([" POL-1 ", date(2026, 1, 2), Decimal("12.5"), None])
    b = hashing.digest(["pol-1", date(2026, 1, 2), Decimal("12.50"), None])
    assert a == b and len(a) == 64
    assert a != hashing.digest(["pol-2", date(2026, 1, 2), Decimal("12.50"), None])


def test_gzipped_binary_word2vec_streams(tmp_path):
    import gzip

    path = tmp_path / "tiny-word2vec.gz"  # the gensim-data packaging of the Google News model
    with gzip.open(path, "wb") as handle:
        handle.write(b"2 3\n")
        for word, vector in (("carrier", [1, 0, 0]), ("insurer", [0.9, 0.1, 0])):
            handle.write(word.encode() + b" " + np.asarray(vector, dtype=np.float32).tobytes() + b"\n")
    table = semantic.load(str(path))
    assert table.similarity(("carrier",), ("insurer",)) > 0.9


def test_a_saved_mapping_to_a_removed_silver_column_must_be_decided_again():
    found, _ = run(["old_col"], saved={"old_col": "column_that_was_dropped"})
    assert found["old_col"].silver_column is None and not found["old_col"].decided
    assert "no longer" in found["old_col"].reason


def test_an_empty_silver_column_list_is_refused(tmp_path):
    empty = tmp_path / "silver_columns.csv"
    empty.write_text("silver_column_name,drt_column_name,data_type,business_key,description\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no Silver columns"):
        catalog.load(empty)


# --- word2vec guards: abbreviations and kinds ---------------------------------------


def _vectors(tmp_path, rows):
    path = tmp_path / "v.txt"
    path.write_text(f"{len(rows)} 3\n" + "\n".join(f"{w} {' '.join(map(str, v))}" for w, v in rows.items()), encoding="utf-8")
    return semantic.load(str(path))


def test_semantic_leaves_abbreviations_to_gemini(tmp_path):
    # "acc" is a real token in news vectors (a sports conference); word2vec must not guess.
    vectors = _vectors(tmp_path, {"acc": [1, 0, 0], "effective": [0, 1, 0], "date": [0, 0, 1],
                                  "policy": [0.2, 1, 0.1], "inception": [0.1, 1, 0.2]})
    found, _ = run(["acc_eff_dt"], similarity=vectors.similarity, semantic_min=0.1)
    assert found["acc_eff_dt"].silver_column is None


def test_an_unknown_word_makes_the_phrase_no_evidence(tmp_path):
    vectors = _vectors(tmp_path, {"effective": [0, 1, 0], "date": [0, 0, 1]})
    assert vectors.similarity(("incep", "effective", "date"), ("effective", "date")) is None


def test_a_code_is_not_a_name_and_an_id_is_not_a_date(tmp_path):
    vectors = _vectors(tmp_path, {"carrier": [1, 0, 0], "insurer": [1, 0.05, 0], "code": [0, 0, 1],
                                  "name": [0, 1, 0], "insurance": [1, 0.1, 0], "company": [0.9, 0.1, 0]})
    found, _ = run(["carrier_code", "policy_id"], similarity=vectors.similarity, semantic_min=0.5)
    assert found["carrier_code"].silver_column is None
    assert found["policy_id"].silver_column != "policy_effective_date"


def test_name_and_code_abbreviations_expand():
    assert normalize.phrase("ins_co_nm") == "insurance company name"
    assert normalize.phrase("carrier_cd") == "carrier code"


def test_azure_openai_voter_uses_structured_outputs(monkeypatch):
    """The Azure call, pinned without a network: system prompt, few-shot user prompt,
    the Answers schema, temperature 0; a refusal surfaces as an error (shown as a note)."""
    import openai

    sent = {}

    class Completions:
        def parse(self, **kwargs):
            sent.update(kwargs)
            answers = llm.Answers(answers=[llm.Answer(bronze_column="acc_eff_dt", reason="acc -> accounting",
                                                      silver_column="accounting_effective_date", alternative=None,
                                                      confidence=1.3)])
            message = type("M", (), {"parsed": answers, "refusal": None})
            return type("C", (), {"choices": [type("Ch", (), {"message": message})]})

    class FakeAzure:
        def __init__(self, **kwargs):
            sent["client"] = kwargs
            self.chat = type("Chat", (), {"completions": Completions()})()

    monkeypatch.setattr(openai, "AzureOpenAI", FakeAzure)
    voter = llm.azure_openai_matcher("k", "https://x.openai.azure.com/", "2025-04-01-preview", "gpt-4.1-mini", False)
    votes = voter({"acc_eff_dt": ["2026-01-31"]}, CATALOG)
    assert votes == {"acc_eff_dt": ("accounting_effective_date", 1.0, "acc -> accounting", None)}  # clamped
    assert sent["model"] == "gpt-4.1-mini" and sent["response_format"] is llm.Answers and sent["temperature"] == 0
    assert sent["messages"][0] == {"role": "system", "content": llm.SYSTEM}
    assert "<examples>" in sent["messages"][1]["content"] and "2026-01-31" not in sent["messages"][1]["content"]
    assert sent["client"]["api_version"] == "2025-04-01-preview"
