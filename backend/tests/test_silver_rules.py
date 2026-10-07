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


def test_saved_mapping_is_reused_but_an_ignore_is_never_preselected():
    # Ignores are not saved; a column without a saved mapping is left for the reviewer.
    found, _ = run(["premium_amt", "notes"], saved={"premium_amt": "premium", "notes": None})
    assert found["premium_amt"].silver_column == "premium" and matching.SAVED in methods(found["premium_amt"])
    assert not found["notes"].ignored and not found["notes"].decided
    assert found["notes"].selection == matching.NONE


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
    assert found["agency"].silver_column == "producer_agency_name"
    assert methods(found["carrier"]) == [matching.SEMANTIC]
    # "agency" also shares a word with producer_agency_name: fuzzy agrees.
    assert matching.SEMANTIC in methods(found["agency"])


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
        return {"writing_agency": ("producer_agency_name", 0.9, "agency that wrote it"),
                "notes": ("made_up_column", 0.99, "invented")}
    found, _ = run(["writing_agency", "notes"], llm=stub)
    assert found["writing_agency"].silver_column == "producer_agency_name"
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


def test_only_two_bronze_columns_on_one_silver_column_block_approval():
    suggestions, _ = matching.suggest(["premium", "premium_amt", "mystery"], CATALOG, {}, {})
    # A bronze column no Silver column takes is simply not loaded: no decision is owed.
    assert not matching.problems("t", suggestions)
    matching.choose(suggestions[1], "premium", False)
    issues = matching.problems("t", suggestions)
    assert issues == ["t: premium, premium_amt all map to premium; keep one."]


def _picks(suggestions):
    return {pick.silver_column: pick for pick in matching.by_silver(suggestions, catalog.targets(CATALOG))}


def test_the_review_from_the_silver_side_has_every_drt_column():
    suggestions, _ = matching.suggest(["written_premium", "premium_total", "carrier", "mystery"], CATALOG, {},
                                      {"carrier": "insurance_company_name"})
    picks = _picks(suggestions)
    assert list(picks) == [column.name for column in catalog.targets(CATALOG)]  # all 48, in table order
    premium = picks["premium"]
    assert premium.bronze_column in ("written_premium", "premium_total") and premium.selection == matching.RECOMMENDED
    assert premium.votes and [bronze for bronze, _ in premium.candidates][0] == premium.bronze_column
    assert {bronze for bronze, _ in premium.candidates} == {"written_premium", "premium_total"}  # both voted, best first
    assert picks["insurance_company_name"].bronze_column == "carrier"
    assert picks["naic_code"].bronze_column is None and picks["naic_code"].selection == matching.NONE


def test_assigning_from_the_silver_side():
    suggestions, _ = matching.suggest(["prem", "net_prem", "eff_dt"], CATALOG, {"prem": "premium"}, {})
    by_name = {s.bronze_column: s for s in suggestions}
    # Another bronze column takes premium: the first one lets go of it.
    matching.assign(suggestions, "premium", "net_prem")
    assert by_name["net_prem"].targets == ["premium"] and "premium" not in by_name["prem"].targets
    assert _picks(suggestions)["premium"].selection == matching.MANUAL
    # One bronze column can load two Silver columns.
    matching.assign(suggestions, "policy_effective_date", "eff_dt")
    matching.assign(suggestions, "accounting_effective_date", "eff_dt")
    assert by_name["eff_dt"].targets == ["policy_effective_date", "accounting_effective_date"]
    # Taking its main one away promotes the other; none leaves the Silver column empty.
    matching.assign(suggestions, "policy_effective_date", None)
    assert by_name["eff_dt"].silver_column == "accounting_effective_date" and by_name["eff_dt"].also == []
    assert _picks(suggestions)["policy_effective_date"].bronze_column is None
    matching.assign(suggestions, "accounting_effective_date", None)
    assert by_name["eff_dt"].targets == [] and by_name["eff_dt"].selection == matching.NONE
    assert not matching.problems("t", suggestions)
    # Choosing the recommended bronze column again restores the recommendation.
    matching.assign(suggestions, "premium", "prem")
    assert _picks(suggestions)["premium"].selection == matching.RECOMMENDED


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
    found, _ = run(["premium"], saved={"premium": "gross_commission_amount"})
    assert found["premium"].silver_column == "gross_commission_amount"  # the reviewer decided this earlier
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
        return {"prod_nm": ("producer_agency_name", 0.6, "prod -> producer or product", "product_line_name")}

    found, _ = run(["prod_nm"], llm=gemini)
    second = by_target(found["prod_nm"])["product_line_name"]
    assert second.support == 0 and second.votes[0].second_choice
    assert found["prod_nm"].silver_column == "producer_agency_name" and not found["prod_nm"].split


def test_picking_the_recommendation_again_restores_it():
    found, _ = run(["premium"])
    suggestion = found["premium"]
    matching.choose(suggestion, "gross_commission_amount", False)
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
    assert examples["carrier_cd"]["silver_column"] == "insurance_company_id"
    assert examples["carr_nm"]["silver_column"] == "insurance_company_name"
    assert examples["insd_nm"]["silver_column"] is None and examples["cncl_dt"]["silver_column"] is None
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

# The business's LOTL shape: (profit_center_number unpadded, legacy_office_name, status).
LOTL = profit_center.Lotl.from_rows([("94", "Dayton Office", "Active"), ("515", "Toledo Office", "Active"),
                                     ("303", "Columbus Office", "Active")])


@pytest.mark.parametrize("name, number, expected", [
    ("Dayton Office", 94, ("Dayton Office", "0094", profit_center.KEPT)),
    ("Toledo Office", None, ("Toledo Office", "0515", profit_center.FILLED_NUMBER)),
    ("Dayton Office", 515, ("Dayton Office", "0094", profit_center.CORRECTED)),
    (None, None, ("Columbus Office", "0303", profit_center.FILLED_NAME)),
    ("Unknown Office", 777, ("Unknown Office", "0777", profit_center.NO_MATCH)),
])
def test_the_four_profit_center_scenarios(name, number, expected):
    assert profit_center.resolve(name, number, "PC0303", LOTL) == expected


def test_without_a_lotl_rows_are_kept_and_flagged():
    assert profit_center.resolve("Dayton Office", 94, "0303", profit_center.Lotl()) == (
        "Dayton Office", "0094", profit_center.UNAVAILABLE)


@pytest.mark.parametrize("value, padded", [(94, "0094"), ("515", "0515"), ("94.0", "0094"), ("PC0001", "PC0001"),
                                           ("", None), ("069_01", "0069_01"), ("0796", "0796")])
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
    # A carrier code is the company's identifier, never its name.
    assert found["carrier_code"].silver_column != "insurance_company_name"
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


# --- the business's LOTL, division and file-name rules ----------------------------------


def test_lotl_prefers_active_rows_and_reads_the_export():
    lotl = profit_center.Lotl.from_rows([
        ("417", "BSPL - Southeast", "Inactive"), ("798", "BSPL - Southeast", "Active"),
        ("null", "ALTRU", "Active"), ("null", "null", "null"), ("069_01", "BSIB-Dallas", "Inactive"),
    ])
    assert lotl.by_name["bspl - southeast"] == "0798"  # the Active row wins
    assert "altru" not in lotl.by_name and lotl.rows == 5  # no number: nothing to look up
    assert lotl.by_number["0069_01"] == ("BSIB-Dallas", "Inactive")
    assert profit_center.resolve(None, None, "PC0798", lotl) == ("BSPL - Southeast", "0798", profit_center.FILLED_NAME)


@pytest.mark.parametrize("name, pc, when", [
    ("PC796_2026-06 796 TPI - AJG Data Submission_796 TPI.xlsx", "PC0796", date(2026, 6, 1)),
    ("ARR_pc0515_2025-06-15.xlsx", "PC0515", date(2025, 6, 15)),
    ("01_ARR_pc0101_2026_JanJun.xlsx", "PC0101", date(2026, 6, 1)),  # months run together: the last
    ("Market_Report_PC2024_Mar_2025.xlsx", "PC2024", date(2025, 3, 1)),  # PC2024 is not a year
    ("PC069_01 202607 data.csv", "PC0069_01", date(2026, 7, 1)),
    ("05_Prem_pc0202_2026.xlsx", "PC0202", None),  # a year alone is not a date
    ("PC796 Q2 2026.xlsx", "PC0796", None),  # a quarter is not a month
    ("PC5_2026-06 5 X - AJG Data Submission_5 X.xlsx", "PC0005", date(2026, 6, 1)),  # "5" is not the day
    ("customer_data.xlsx", None, None),
])
def test_pc_id_and_file_date_come_from_the_file_name(name, pc, when):
    from ahi_bronze import file_meta

    assert file_meta.pc_id_from_filename(name) == pc
    assert file_meta.file_date_from_filename(name) == when


@pytest.mark.parametrize("value, normalized", [("796", "PC0796"), ("PC796", "PC0796"), ("pc0796", "PC0796"),
                                               ("0796.0", "PC0796"), ("PC069_01", "PC0069_01"), ("null", None),
                                               ("abc", None), (None, None)])
def test_pc_ids_are_normalized(value, normalized):
    from ahi_bronze import file_meta

    assert file_meta.normalize_pc_id(value) == normalized


def test_division_lookup_returns_every_distinct_division():
    from ahi_bronze import file_meta

    rows = [("AH Programs", "No", "0673"), ("AH Specialty", "No", "0673"), ("Bridge Specialty Group", "No", "0796"),
            ("Bridge Specialty Group", "No", "0796"), ("AH Specialty", "No", "0N/A")]
    assert file_meta.divisions_for("PC0796", rows) == ["Bridge Specialty Group"]  # duplicates collapse
    assert file_meta.divisions_for("PC0673", rows) == ["AH Programs", "AH Specialty"]  # the reviewer picks
    assert file_meta.divisions_for("PC0101", rows) == []
    rows.append(("Bridge Specialty Group", "No", "0069"))
    assert file_meta.divisions_for("PC0069_01", rows) == ["Bridge Specialty Group"]  # a sub-office


def test_the_catalog_is_the_business_silver_schema():
    names = [column.name for column in CATALOG]
    assert len(names) == 69 and names[0] == "ahi_policy_transaction_id" and names[-1] == "ajg_apd"
    types = {column.name: column for column in CATALOG}
    assert types["commission_pct"].sql_type == "numeric(10,6)" and types["premium"].sql_type == "numeric(18,2)"
    assert types["is_mga"].sql_type == "boolean" and types["ingestion_timestamp"].sql_type == "timestamptz"
    assert types["source_file"].role == catalog.SYSTEM and types["premium"].role == catalog.MAPPED
    # Only mapped columns are ever offered as targets.
    found, _ = run(["row_hash", "source_file"])
    assert all(s.silver_column is None for s in found.values())


# The business's DRT columns, as the business writes them, and the Silver column each loads.
DRT_COLUMNS = {
    "Policy Transaction ID": "policy_tran_id", "Profit Center Name": "profit_center_name",
    "Program Name": "program_name", "Producer Code": "producer_code",
    "Producer E&O Insurance Company Name": "producer_eo_insurance_company_name",
    "Insurance Company AM Best Number": "insurance_company_am_best_number",
    "Producer Office City": "producer_office_city", "Policy Expiration Date": "policy_expiration_date",
    "TransactionEffectiveDate <for different transactions>": "transaction_effective_date",
    "Producer License Number": "producer_license_number", "PolicyNumber": "policy_number",
    "Producer Billing Zipcode": "producer_billing_zipcode", "Producer Billing Address": "producer_billing_address",
    "Profit Center Number": "profit_center_number", "Parent Producer Name": "parent_producer_name",
    "GrossCommissionAmount": "gross_commission_amount", "AccountingEffective Date": "accounting_effective_date",
    "Is MGA": "is_mga", "Producer Billing City": "producer_billing_city", "InsuranceCompanyID": "insurance_company_id",
    "Parent Producer ID": "parent_producer_id", "Policy Status": "policy_status",
    "ProducerCommissionAmount": "producer_commission_amount", "Producer Expiration Date": "producer_expiration_date",
    "Producer/Agency Name": "producer_agency_name", "Producer E&O Policy Number": "producer_eo_policy_number",
    "NAIC Code": "naic_code", "Producer Effective Date": "producer_effective_date",
    "Producer Billing State": "producer_billing_state", "Producer Office Location": "producer_office_location",
    "Product Line Name": "product_line_name", "PolicyEffectiveYear": "policy_effective_year",
    "MarketProviderID": "market_provider_id", "PolicyFees": "policy_fees",
    "Producer Office Zipcode": "producer_office_zipcode", "Producer Office State": "producer_office_state",
    "MarketProvider": "market_provider", "Is MGU": "is_mgu", "PolicyEffectiveMonth": "policy_effective_month",
    "ProducerCommissionPct": "producer_commission_pct", "PolicyEffectiveDate": "policy_effective_date",
    "InsuranceCompany Name": "insurance_company_name", "CommissionPct": "commission_pct", "Premium": "premium",
    "Revenue": "revenue", "Renewal Flag": "renewal_flag", "Transaction Detail": "transaction_detail",
    "Producer Tax ID": "producer_tax_id",
}


def test_the_mapping_targets_are_exactly_the_business_drt_columns():
    targets = catalog.targets(CATALOG)
    assert len(DRT_COLUMNS) == 48 and {column.drt_name: column.name for column in targets} == DRT_COLUMNS
    assert all(catalog.SNAKE_CASE.fullmatch(column.name) for column in targets)
    # Columns of the Silver table that are not DRT columns stay in the table (and its row
    # hash) but are never offered.
    by_name = {column.name: column for column in CATALOG}
    for name in ("UltimateParentProducerNames", "ajg_apd"):
        assert by_name[name].role == catalog.MAPPED and not by_name[name].is_target
    assert not any(column.is_target for column in CATALOG if column.role == catalog.SYSTEM)


def test_a_source_column_named_like_a_non_drt_column_is_never_proposed_for_it():
    found, _ = run(["ajg_apd", "UltimateParentProducerNames", "Ultimate Parent Producer Names"])
    proposed = {candidate.silver_column for s in found.values() for candidate in s.candidates}
    assert not proposed & {"ajg_apd", "UltimateParentProducerNames"}
    # A saved mapping to a column the mapping no longer offers is not reused, and says why.
    stale, _ = run(["apd"], saved={"apd": "ajg_apd"})
    assert stale["apd"].silver_column is None and "no longer a DRT column" in stale["apd"].reason


def _catalog_file(tmp_path, rows):
    path = tmp_path / "silver.csv"
    path.write_text("silver_column_name,drt_column_name,data_type,business_key,role,description\n" + "\n".join(rows),
                    encoding="utf-8")
    return path


def test_a_drt_column_must_be_named_lowercase_with_underscores(tmp_path):
    with pytest.raises(ValueError, match="lowercase_with_underscores; rename PolicyNumber"):
        catalog.load(_catalog_file(tmp_path, ["PolicyNumber,PolicyNumber,string,y,mapped,", "premium,Premium,string,n,mapped,"]))
    # A Silver column that is not a DRT column may keep the schema's own spelling.
    loaded = catalog.load(_catalog_file(tmp_path, ["premium,Premium,string,n,mapped,",
                                                   "UltimateParentProducerNames,,string,n,mapped,"]))
    assert [column.name for column in catalog.targets(loaded)] == ["premium"]


def test_two_silver_columns_cannot_share_a_drt_column(tmp_path):
    with pytest.raises(ValueError, match="same DRT column"):
        catalog.load(_catalog_file(tmp_path, ["premium,Premium,string,n,mapped,", "net_premium,PREMIUM,string,n,mapped,"]))
    with pytest.raises(ValueError, match="no DRT columns"):
        catalog.load(_catalog_file(tmp_path, ["premium,,string,n,mapped,", "row_hash,,string,n,system,"]))


def test_every_drt_label_has_a_silver_column():
    for label in ["AccountingEffective Date", "InsuranceCompany Name", "Producer/Agency Name", "CommissionPct",
                  "TransactionEffectiveDate <for different transactions>", "Producer Tax ID"]:
        assert catalog.silver_name_for_drt(label, CATALOG) is not None, label


def test_typed_cleansing_follows_each_column_type():
    columns = {column.name: column for column in CATALOG}
    # A percent sign divides by 100, so every source lands on one scale.
    rate, bad = cleanse.by_type(pl.Series("x", ["12.5%", "0.123456", "abc"]), columns["commission_pct"])
    assert rate.to_list() == [Decimal("0.125000"), Decimal("0.123456"), None] and bad == 1
    month, _ = cleanse.by_type(pl.Series("x", ["Jun", "7", "July"]), columns["policy_effective_month"])
    assert month.to_list() == [6, 7, 7]
    flag, bad = cleanse.by_type(pl.Series("x", ["Y", "no", "maybe"]), columns["is_mga"])
    assert flag.to_list() == [True, False, None] and bad == 1


def test_one_bronze_column_can_load_two_silver_columns():
    found, _ = run(["effectivedate"], saved={"effectivedate": ["policy_effective_date", "accounting_effective_date"]})
    suggestion = found["effectivedate"]
    assert suggestion.targets == ["policy_effective_date", "accounting_effective_date"] or \
        suggestion.targets == ["accounting_effective_date", "policy_effective_date"]
    assert not suggestion.split  # a second target is not a disagreement
    assert not matching.problems("t", [suggestion])


def test_two_headers_that_normalize_alike_compete_instead_of_both_loading():
    # "Agent Commission" (amount) and "Agent Commission%" (rate) are one key once normalized.
    saved = {"agent_commission": ["producer_commission_amount", "producer_commission_pct"]}
    found, _ = run(["agent_commission"], saved=saved, one_to_many=set())
    assert found["agent_commission"].also == [] and found["agent_commission"].split


def test_an_extra_target_counts_as_taken():
    found, _ = run(["premium", "net_premium"])
    matching.set_also(found["premium"], ["revenue"])
    matching.choose(found["net_premium"], "revenue", False)
    assert any("all map to revenue" in issue for issue in matching.problems("t", list(found.values())))


def test_transform_writes_exactly_the_silver_schema():
    from datetime import datetime, timezone

    from ahi_silver import transform

    frame = pl.DataFrame({"policyeffectivedate": ["2026-03-05"], "premium_amt": ["$1,200.50"], "rate": ["12%"],
                          "_ingestion_id": ["i1"], "_source_file": ["f.xlsx"], "_source_sheet": ["S"]})
    stamp = datetime(2026, 7, 1, 10, tzinfo=timezone.utc)
    context = transform.Context("pc0101", "bronze.ext_pc0101_arr", stamp,
                                {"i1": transform.LoadInfo("f.xlsx", stamp, "2026-01", "2026-06")})
    mapping = [("policyeffectivedate", "policy_effective_date"), ("policyeffectivedate", "accounting_effective_date"),
               ("premium_amt", "premium"), ("rate", "commission_pct")]
    silver, quality = transform.transform(frame, mapping, CATALOG, "PC0101", profit_center.Lotl(), context)
    assert silver.columns == [column.name for column in CATALOG][1:]  # all but the generated key
    row = silver.row(0, named=True)
    assert row["policy_effective_date"] == row["accounting_effective_date"] == date(2026, 3, 5)
    assert (row["policy_effective_year"], row["policy_effective_month"]) == (2026, 3)  # derived
    assert row["premium"] == Decimal("1200.50") and row["commission_pct"] == Decimal("0.120000")
    assert (row["source_table"], row["source_file"], row["ingestion_timestamp"]) == ("bronze.ext_pc0101_arr", "f.xlsx", stamp)
    assert (row["source_data_period_start_date"], row["source_data_period_end_date"],
            row["source_data_period_type"]) == (date(2026, 1, 1), date(2026, 6, 30), "DATE_RANGE")
    assert row["row_hash"] and row["business_key_hash"] and quality["rows"] == 1


# --- cleansing edge cases ------------------------------------------------------------


def test_a_two_digit_year_is_not_a_date_and_serials_are_bounded():
    values = ["1/5/26", "99999", "12345678", "2026-01-05 13:45:00.123", "2026-01-05T08:00:00", "9999-12-31"]
    parsed, invalid = cleanse.dates(pl.Series("d", values))
    assert parsed.to_list() == [None, None, None, date(2026, 1, 5), date(2026, 1, 5), date(9999, 12, 31)]
    assert invalid == 3


def test_decimals_are_read_exactly_and_strictly():
    values = ["$(250.00)", "(12%)", "1.005", "0.125", "1 200,50", "USD 100", "1,2345", "1.2E+03"]
    parsed, invalid = cleanse.decimals(pl.Series("p", values), 18, 2)
    assert parsed.to_list() == [Decimal("-250.00"), Decimal("-0.12"), Decimal("1.01"), Decimal("0.13"),
                                None, None, None, Decimal("1200.00")]
    assert invalid == 3


def test_amounts_keep_their_cents_beyond_float_precision():
    parsed, _ = cleanse.decimals(pl.Series("p", ["9999999999999999.99"]), 18, 2)
    assert parsed.to_list() == [Decimal("9999999999999999.99")]


def test_a_percent_is_not_a_whole_number():
    parsed, invalid = cleanse.integers(pl.Series("y", ["2026", "2026.0", "12%", "2,026"]))
    assert parsed.to_list() == [2026, 2026, None, 2026] and invalid == 1


# --- profit center edge cases --------------------------------------------------------


def test_text_null_is_missing_and_pc_numbers_are_read():
    assert profit_center.resolve("null", "N/A", "PC0303", LOTL) == (
        "Columbus Office", "0303", profit_center.FILLED_NAME)
    assert profit_center.resolve("Dayton Office", "PC0094", "PC0303", LOTL) == (
        "Dayton Office", "0094", profit_center.KEPT)


def test_lotl_names_match_across_spacing_case_and_dashes():
    lotl = profit_center.Lotl.from_rows([("423", "BSG California – legacy Hull Stockton", "Active"),
                                         ("360", "N/A do not show\nCorp-Accession (Bridge)", "Active")])
    assert profit_center.resolve("bsg california - legacy  hull stockton", None, None, lotl)[1:] == (
        "0423", profit_center.FILLED_NUMBER)
    assert profit_center.resolve("N/A do not show Corp-Accession (Bridge)", None, None, lotl)[1] == "0360"


def test_each_row_takes_its_own_loads_profit_center():
    from ahi_silver import transform

    frame = pl.DataFrame({"premium_amt": ["1", "2"], "_ingestion_id": ["a", "b"],
                          "_source_file": ["a.xlsx", "b.xlsx"], "_source_sheet": [None, None]})
    context = transform.Context("pc0094", "bronze.ext_pc0094_arr", None, {
        "a": transform.LoadInfo("a.xlsx", pc_id="PC0094"), "b": transform.LoadInfo("b.xlsx", pc_id="PC0515")})
    silver, quality = transform.transform(frame, [("premium_amt", "premium")], CATALOG, "PC0094", LOTL, context,
                                          extras=True)
    rows = silver.select(["profit_center_name", "profit_center_number", "_ingestion_id", "_pc_status"]).rows()
    assert rows == [("Dayton Office", "0094", "a", "filled_name"), ("Toledo Office", "0515", "b", "filled_name")]


def test_the_cleansed_extras_name_the_unreadable_values():
    from ahi_silver import transform

    frame = pl.DataFrame({"premium_amt": ["1", "abc"], "eff": ["bad", "2026-01-02"], "_ingestion_id": ["a", "a"],
                          "_source_file": ["a.xlsx"] * 2, "_source_sheet": [None, None]})
    silver, _ = transform.transform(frame, [("premium_amt", "premium"), ("eff", "policy_effective_date")], CATALOG,
                                    "PC0094", LOTL, transform.Context(), extras=True)
    assert silver.columns[-3:] == transform.EXTRAS
    assert silver["_invalid_columns"].to_list() == [["policy_effective_date"], ["premium"]]
