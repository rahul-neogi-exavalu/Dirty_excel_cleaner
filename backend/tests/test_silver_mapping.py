"""The DRT column mapping rules of the Silver service that need no database:
which saved rows a bronze column meets, which rows an approval may replace, and how a
review is stored."""

from datetime import date, datetime, timezone

from ahi_silver import matching

from api.services import silver_service as service

ROWS = [
    ("PC0692", "Agent Commission", "ProducerCommissionAmount", "producer_commission_amount"),
    ("PC0692", "Agent Commission%", "ProducerCommissionPct", "producer_commission_pct"),
    ("PC0692", "Net Premium", "Premium", "premium"),
    ("PC0692", "EffectiveDate", "PolicyEffectiveDate", "policy_effective_date"),
    ("PC0692", "EffectiveDate", "AccountingEffective Date", "accounting_effective_date"),
    ("PC0094", "Carrier", "InsuranceCompany Name", "insurance_company_name"),
]


def test_a_column_meets_its_saved_row_by_the_header_it_was_written_with():
    columns = ["agent_commission", "agent_commission_2"]
    headers = {"agent_commission": "Agent Commission", "agent_commission_2": "Agent Commission%"}
    own, _, _ = service._saved(ROWS, ["PC0692"], columns, headers)
    assert own == {"agent_commission": ["producer_commission_amount"],
                   "agent_commission_2": ["producer_commission_pct"]}


def test_without_a_header_a_column_meets_its_row_on_normalized_words():
    own, known, one_to_many = service._saved(ROWS, ["PC0692"], ["net_premium", "effectivedate"])
    assert own["net_premium"] == ["premium"]
    assert own["effectivedate"] == ["policy_effective_date", "accounting_effective_date"]
    assert one_to_many == {"effectivedate"}
    assert known  # another profit center's approval is a vote, not this table's own


def test_every_profit_center_of_the_table_counts_as_its_own():
    own, _, _ = service._saved(ROWS, ["PC0692", "PC0094"], ["carrier"])
    assert own == {"carrier": ["insurance_company_name"]}


def test_an_approval_replaces_only_the_same_header():
    mine = [row for row in ROWS if row[0] == "PC0692"]
    assert service._current_rows(mine, "Agent Commission") == [ROWS[0]]
    assert service._current_rows(mine, "agent  commission%") == [ROWS[1]]
    # Two different headers with the same words: neither is "the" column.
    assert service._current_rows(mine, "agent_commission") == []
    assert service._current_rows(mine, "net_premium") == [ROWS[2]]


def test_ignores_are_never_examples_for_the_ai():
    rows = ROWS + [("PC0692", "Notes", None, None)]
    assert all(silver for _, silver in service._precedents(rows, "PC0692"))


def test_a_review_is_stored_and_read_back_whole():
    suggestion = matching.Suggestion(
        "net_premium", "premium", selection=matching.RECOMMENDED, also=["revenue"], samples=["1"],
        candidates=[matching.Candidate("premium", [matching.Vote(matching.SAVED, "premium", 1.0, "saved", own=True)], True)])
    load = service.Load("i1", "f.xlsx", 3, "2026-01", "2026-06", pc_id="PC0692",
                        processing_date=datetime(2026, 7, 1, tzinfo=timezone.utc), file_date=date(2026, 6, 30))
    review = service.TableReview("ext_pc0692_arr", "pc0692", "PC0692", [load], ["net_premium"], [suggestion],
                                 {"rows": 3}, ["PC0692"], {"net_premium": "Net Premium"})
    run = service.Run("r1", [review], ["note"], [], lotl_rows=12, loaded={"i1": 3})
    state = service._state(run)
    stamp = datetime(2026, 7, 2, tzinfo=timezone.utc)
    back, _ = service._from_row(("r1", "draft", state, 2, 0.0, "", None, None, None, None, stamp, None, None, stamp, None))
    assert back.tables[0].loads[0] == load
    assert back.tables[0].suggestions[0] == suggestion
    assert back.tables[0].headers == {"net_premium": "Net Premium"} and back.tables[0].pc_ids == ["PC0692"]
    assert (back.lotl_rows, back.loaded, back.version) == (12, {"i1": 3}, 2)
