"""The business's rules for whether a cleaned file may reach Bronze (no database)."""

from datetime import date

import polars as pl
import pytest

from ahi_bronze import file_meta, validation as v


def _frame(aed, ped=None, ted=None):
    data = {"Acct Eff Dt": aed}
    if ped is not None:
        data["Pol_Ef_Dt"] = ped
    if ted is not None:
        data["Trans Eff"] = ted
    return pl.DataFrame(data, schema={name: pl.String for name in data})


COLUMNS = {v.AED: "Acct Eff Dt", v.PED: "Pol_Ef_Dt", v.TED: "Trans Eff"}


def test_the_required_columns_are_the_business_list():
    from api import config

    required = v.load_required(config.BRONZE_REQUIRED_COLUMNS_FILE)
    assert len(required) == 14
    roles = {item.date_role: item.name for item in required if item.date_role}
    assert roles == {"AED": "accounting_effective_date", "PED": "policy_effective_date",
                     "TED": "transaction_effective_date"}
    assert {item.label for item in required} >= {"Producer/Agency Name", "InsuranceCompany Name", "Revenue"}
    pairs = [tuple(item.label for item in group) for group in v.requirements(required) if len(group) > 1]
    assert pairs == [("CommissionPct", "GrossCommissionAmount"), ("ProducerCommissionAmount", "ProducerCommissionPct")]
    assert len(v.requirements(required)) == 12


def test_one_of_a_group_of_alternatives_is_enough():
    required = [v.Required("revenue", "Revenue"), v.Required("commission_pct", "CommissionPct", one_of="gross"),
                v.Required("gross_commission_amount", "GrossCommissionAmount", one_of="gross")]
    assert v.missing_required(required, {"revenue", "commission_pct"}) == []
    assert v.missing_required(required, {"revenue", "gross_commission_amount"}) == []
    assert v.missing_required(required, {"revenue", "commission_pct", "gross_commission_amount"}) == []
    assert v.missing_required(required, {"commission_pct"}) == ["Revenue"]
    assert v.missing_required(required, set()) == ["Revenue", "CommissionPct or GrossCommissionAmount"]


def test_a_sheet_is_rejected_with_its_file():
    decision = v.rejected_with_file({"Feb": "Missing required column: Revenue."})
    assert decision.action == v.REJECTED
    assert decision.reasons == ["Rejected with its file: every data sheet of a file must be fit for Bronze, and "
                                "Feb (Missing required column: Revenue) is not."]
    two = v.rejected_with_file({"Feb": "Rejected by the reviewer.", "Mar": "Flagged."})
    assert two.reasons[0].endswith("Feb (Rejected by the reviewer); Mar (Flagged) are not.")


def test_aed_decides_when_every_row_has_one():
    frame = _frame(["2026-01-03", "02/10/2026", "2026-06-27"], ["2025-07-01"] * 3, ["2026-01-01"] * 3)
    stats = v.date_stats(frame, COLUMNS)
    chosen = v.reporting_source(stats)
    assert chosen.role == v.AED and (chosen.first, chosen.last) == (date(2026, 1, 3), date(2026, 6, 27))
    assert stats[v.AED].percent == 100.0


def test_99_of_100_is_not_every_row_so_ted_decides():
    aed = ["2026-03-15"] * 99 + [None]
    frame = _frame(aed, ["2026-03-01"] * 100, ["2026-03-02"] * 100)
    stats = v.date_stats(frame, COLUMNS)
    assert stats[v.AED].populated == 99 and not stats[v.AED].complete
    assert v.reporting_source(stats).role == v.TED


def test_the_priority_is_aed_then_ted_then_ped():
    assert v.DATE_ORDER == (v.AED, v.TED, v.PED)
    # PED decides only when neither AED nor TED is populated on every row.
    frame = _frame(["2026-03-15", None], ["2026-03-01", "2026-03-01"], ["2026-03-02", None])
    assert v.reporting_source(v.date_stats(frame, COLUMNS)).role == v.PED


def test_each_complete_date_column_has_its_range_and_kind():
    # AED runs Feb-Jun (neither YTD nor monthly); TED Jan-Jun (YTD); PED one month.
    frame = _frame(["2026-02-03", "2026-06-20"], ["2025-07-01", "2025-07-15"], ["2026-01-09", "2026-06-02"])
    ranges = v.role_ranges(v.date_stats(frame, COLUMNS))
    assert list(ranges) == [v.AED, v.TED, v.PED]
    assert ranges[v.TED] == {"start": date(2026, 1, 1), "end": date(2026, 6, 30), "period_type": v.YTD, "flag": None}
    assert ranges[v.AED]["period_type"] is None and ranges[v.AED]["flag"]
    assert ranges[v.PED]["period_type"] == v.MONTHLY
    # Only a column populated on every row has a range.
    partial = _frame(["2026-02-03", None], ["2025-07-01", "2025-07-15"], ["2026-01-09", "2026-06-02"])
    assert v.AED not in v.role_ranges(v.date_stats(partial, COLUMNS))


def test_entered_dates_take_the_date_column_whose_range_they_are():
    frame = _frame(["2026-02-03", "2026-06-20"], ["2026-01-01", "2026-06-15"], ["2026-01-09", "2026-06-02"])
    stats = v.date_stats(frame, COLUMNS)
    # TED and PED both run Jan-Jun: TED comes first.
    assert v.matching_role(stats, date(2026, 1, 1), date(2026, 6, 30)) == v.TED
    assert v.matching_role(stats, date(2026, 2, 1), date(2026, 6, 30)) == v.AED
    # Dates no column spans: blank.
    assert v.matching_role(stats, date(2026, 1, 1), date(2026, 5, 31)) is None
    # A column missing a row never matches, even when its dates would.
    partial = v.date_stats(_frame(["2026-02-03", None], ["2026-01-01", "2026-06-15"], None),
                           {v.AED: "Acct Eff Dt", v.PED: "Pol_Ef_Dt", v.TED: None})
    assert v.matching_role(partial, date(2026, 2, 1), date(2026, 2, 28)) is None


def test_an_unreadable_value_counts_as_not_populated():
    frame = _frame(["2026-01-05", "TBD"], ["n/a", "2026-01-01"], ["2026-01-09", "2026-01-10"])
    stats = v.date_stats(frame, COLUMNS)
    assert stats[v.AED].invalid == 1 and stats[v.PED].invalid == 1
    assert v.reporting_source(stats).role == v.TED


def test_no_fully_populated_date_leaves_it_to_the_reviewer():
    frame = _frame(["2026-01-05", None], [None, None], None)
    stats = v.date_stats(frame, {v.AED: "Acct Eff Dt", v.PED: "Pol_Ef_Dt", v.TED: None})
    assert v.reporting_source(stats) is None
    assert stats[v.TED].column is None and not stats[v.TED].complete


@pytest.mark.parametrize("start, end, kind, flagged", [
    (date(2026, 1, 1), date(2026, 6, 30), v.YTD, False),
    (date(2026, 6, 1), date(2026, 6, 30), v.MONTHLY, False),
    (date(2026, 1, 1), date(2026, 1, 31), v.MONTHLY, False),  # January alone is a month
    (date(2026, 2, 1), date(2026, 6, 30), None, True),  # not from January
    (date(2024, 1, 1), date(2026, 6, 30), None, True),  # across years
])
def test_year_to_date_or_monthly(start, end, kind, flagged):
    found, flag = v.classify(start, end)
    assert found == kind and bool(flag) == flagged


def test_reporting_dates_are_whole_months():
    assert v.month_start(date(2026, 1, 3)) == date(2026, 1, 1)
    assert v.month_end(date(2026, 2, 10)) == date(2026, 2, 28)
    assert v.month_end(date(2028, 2, 10)) == date(2028, 2, 29)


def test_month_stats_count_rows_and_population_per_month():
    frame = _frame(["2026-05-02", "2026-06-01", "2026-06-30"], ["2026-01-01", None, "2026-01-01"], None)
    months = v.row_months(frame, "Acct Eff Dt")
    stats = v.month_stats(frame, months, {v.AED: "Acct Eff Dt", v.PED: "Pol_Ef_Dt", v.TED: None})
    assert stats["2026-06"] == {"rows": 2, "AED": 100.0, "PED": 50.0, "TED": None}
    assert stats["2026-05"]["rows"] == 1
    assert v.row_months(frame, None, "2026-07").to_list() == ["2026-07"] * 3


def _loaded(cid, name, start, end, kind=v.YTD):
    return v.Loaded(cid, name, kind, start, end)


H1 = _loaded(1, "jan-jun.xlsx", date(2026, 1, 1), date(2026, 6, 30))
JULY = _loaded(2, "july.xlsx", date(2026, 7, 1), date(2026, 7, 31), v.MONTHLY)


def _act(kind, start, end, loaded, choice=None, missing=(), flag=None):
    return v.business_action(missing=list(missing), flag=flag, period_type=kind, start=start, end=end,
                             loaded=loaded, choice=choice)


def test_first_file_of_the_year_is_inserted():
    assert _act(v.YTD, date(2026, 1, 1), date(2026, 6, 30), []).action == v.INSERT
    assert _act(v.MONTHLY, date(2026, 7, 1), date(2026, 7, 31), []).action == v.INSERT
    # Last year's files are another year.
    old = _loaded(9, "2025.xlsx", date(2025, 1, 1), date(2025, 12, 31))
    assert _act(v.YTD, date(2026, 1, 1), date(2026, 3, 31), [old]).action == v.INSERT


def test_a_later_year_to_date_file_replaces_the_year():
    decision = _act(v.YTD, date(2026, 1, 1), date(2026, 8, 31), [H1, JULY])
    assert decision.action == v.INSERT and decision.confirm
    assert [item.control_id for item in decision.replaces] == [1, 2]
    assert decision.file_replaced() == "jan-jun.xlsx, july.xlsx"


def test_an_older_year_to_date_file_is_the_reviewers_call():
    decision = _act(v.YTD, date(2026, 1, 1), date(2026, 5, 31), [H1])
    assert decision.action == v.DECIDE and decision.options == [v.REPLACE, v.REJECT]
    assert _act(v.YTD, date(2026, 1, 1), date(2026, 5, 31), [H1], choice=v.REPLACE).action == v.INSERT
    assert _act(v.YTD, date(2026, 1, 1), date(2026, 5, 31), [H1], choice=v.REJECT).action == v.REJECTED


def test_the_same_reporting_dates_are_a_revision_or_a_companion_for_the_reviewer():
    # Jan-Jun again, after Jan-Jun and July: the reviewer says what it is.
    decision = _act(v.YTD, date(2026, 1, 1), date(2026, 6, 30), [H1, JULY])
    assert decision.action == v.DECIDE and decision.options == [v.REVISE, v.COMPANION, v.REJECT]
    assert decision.overlaps == [H1] and "jan-jun.xlsx already has Jan–Jun 2026" in decision.reasons[0]
    # A month already loaded on its own is asked the same way.
    assert _act(v.MONTHLY, date(2026, 7, 1), date(2026, 7, 31), [H1, JULY]).options == [v.REVISE, v.COMPANION, v.REJECT]


def test_a_revision_replaces_the_named_file_and_keeps_the_later_months():
    decision = v.business_action(missing=[], flag=None, period_type=v.YTD, start=date(2026, 1, 1),
                                 end=date(2026, 6, 30), loaded=[H1, JULY], choice=v.REVISE, revises=H1)
    assert decision.action == v.INSERT and decision.confirm and decision.replaces == [H1]
    assert decision.file_replaced() == "jan-jun.xlsx"
    assert decision.reasons[0] == "Revision: replaces jan-jun.xlsx (Jan–Jun 2026)."
    assert any("Keeps the later july.xlsx" in reason for reason in decision.reasons)


def test_a_revision_may_name_only_a_file_sharing_a_month():
    assert v.overlapping([H1, JULY], date(2026, 6, 1), date(2026, 6, 30)) == [H1]
    assert v.overlapping([H1, JULY], date(2026, 8, 1), date(2026, 8, 31)) == []


def test_a_new_month_is_appended():
    decision = _act(v.MONTHLY, date(2026, 7, 1), date(2026, 7, 31), [H1])
    assert decision.action == v.APPEND and not decision.confirm and decision.file_replaced() is None
    gap = _act(v.MONTHLY, date(2026, 9, 1), date(2026, 9, 30), [H1, JULY])
    assert gap.action == v.APPEND and any("Aug 2026" in reason for reason in gap.reasons)


def test_a_month_already_loaded_is_flagged_and_rejected():
    # June again, after the Jan-Jun year-to-date file: the control table rejects it.
    decision = _act(v.MONTHLY, date(2026, 6, 1), date(2026, 6, 30), [H1, JULY])
    assert decision.action == v.REJECTED and decision.flagged and decision.overlaps == [H1]
    assert decision.reasons[0].startswith("Jun 2026 is already loaded from jan-jun.xlsx (Jan–Jun 2026)")
    assert decision.options == []


def test_a_month_after_the_year_to_date_file_appends_to_it():
    decision = _act(v.MONTHLY, date(2026, 7, 1), date(2026, 7, 31), [H1])
    assert decision.action == v.APPEND and decision.appends == [H1]
    assert "same bronze table as jan-jun.xlsx" in decision.reasons[0]


def test_a_sheet_naming_one_month_gives_its_rows_that_month():
    assert v.sheet_month("January 2026") == "2026-01" and v.sheet_month("Jun 2026") == "2026-06"
    assert v.sheet_month("Jan-Jun 2026") is None and v.sheet_month("Sheet1") is None
    assert v.sheet_months(["January 2026", "March 2026"]) == {"January 2026": "2026-01", "March 2026": "2026-03"}
    assert v.sheet_months(["January 2026", "Notes"]) is None
    frame = pl.DataFrame({"source_sheet": ["January 2026", "March 2026", None]})
    assert v.row_months(frame, None, sheets=("source_sheet", {"January 2026": "2026-01", "March 2026": "2026-03"})
                        ).to_list() == ["2026-01", "2026-03", None]


def test_missing_columns_or_an_uncorrected_flag_reject_the_file():
    missing = _act(v.YTD, date(2026, 1, 1), date(2026, 6, 30), [], missing=["Revenue", "MarketProvider"])
    assert missing.action == v.REJECTED and "Revenue, MarketProvider" in missing.reasons[0]
    flagged = _act(None, None, None, [], flag="Feb–Jun 2026 is neither year-to-date nor a single month.")
    assert flagged.action == v.REJECTED


@pytest.mark.parametrize("name, when, noted", [
    ("PC8051_2026 H1 Arrowhead Transaction Data Report_07132026.xlsx", date(2026, 7, 13), False),
    ("PC2047_IRM BB Monthly Policy Data Request 20260731_08042026.xlsx", date(2026, 8, 4), True),  # the last date
    ("PC2000_combinedTransactionFile_20260921145504095_20260922_v2.xlsx", date(2026, 9, 22), False),
    ("x_13072026.xlsx", date(2026, 7, 13), True),  # only day-first fits
    ("x_20260713.xlsx", date(2026, 7, 13), False),
    ("x_2026-07-13.xlsx", date(2026, 7, 13), False),
    ("x_7.13.2026.xlsx", date(2026, 7, 13), False),
    ("PC2043_AJG Data Request 010126-063026.xlsx", None, False),  # two-digit years: the reviewer enters it
    ("PC796_2026-06 796 TPI - AJG Data Submission_796 TPI.xlsx", date(2026, 6, 1), True),  # a month only
])
def test_the_file_received_date_is_normalised_from_the_name(name, when, noted):
    found, note = file_meta.received_date_from_filename(name)
    assert found == when and bool(note) == noted
