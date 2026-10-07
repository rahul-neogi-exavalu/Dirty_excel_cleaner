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


def test_aed_decides_when_every_row_has_one():
    frame = _frame(["2026-01-03", "02/10/2026", "2026-06-27"], ["2025-07-01"] * 3, ["2026-01-01"] * 3)
    stats = v.date_stats(frame, COLUMNS)
    chosen = v.reporting_source(stats)
    assert chosen.role == v.AED and (chosen.first, chosen.last) == (date(2026, 1, 3), date(2026, 6, 27))
    assert stats[v.AED].percent == 100.0


def test_99_of_100_is_not_every_row_so_ped_decides():
    aed = ["2026-03-15"] * 99 + [None]
    frame = _frame(aed, ["2026-03-01"] * 100, ["2026-03-02"] * 100)
    stats = v.date_stats(frame, COLUMNS)
    assert stats[v.AED].populated == 99 and not stats[v.AED].complete
    assert v.reporting_source(stats).role == v.PED


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


def test_a_revised_year_to_date_file_keeps_the_later_months():
    # Jan-Jun revised after July was appended: Jan-Jun is replaced, July stays.
    decision = _act(v.YTD, date(2026, 1, 1), date(2026, 6, 30), [H1, JULY])
    assert decision.action == v.INSERT and [item.control_id for item in decision.replaces] == [1]
    assert decision.file_replaced() == "jan-jun.xlsx" and any("Keeps the later" in r for r in decision.reasons)


def test_a_new_month_is_appended():
    decision = _act(v.MONTHLY, date(2026, 7, 1), date(2026, 7, 31), [H1])
    assert decision.action == v.APPEND and not decision.confirm and decision.file_replaced() is None
    gap = _act(v.MONTHLY, date(2026, 9, 1), date(2026, 9, 30), [H1, JULY])
    assert gap.action == v.APPEND and any("Aug 2026" in reason for reason in gap.reasons)


def test_a_month_already_loaded_is_the_reviewers_call():
    decision = _act(v.MONTHLY, date(2026, 6, 1), date(2026, 6, 30), [H1, JULY])
    assert decision.action == v.DECIDE and decision.overlaps == [H1]
    assert decision.options == [v.REPLACE_MONTH, v.REJECT]
    replaced = _act(v.MONTHLY, date(2026, 6, 1), date(2026, 6, 30), [H1, JULY], choice=v.REPLACE_MONTH)
    assert replaced.action == v.APPEND and replaced.replace_month == "2026-06" and replaced.confirm
    assert replaced.file_replaced() == "jan-jun.xlsx"
    rejected = _act(v.MONTHLY, date(2026, 6, 1), date(2026, 6, 30), [H1], choice=v.REJECT)
    assert rejected.action == v.REJECTED


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
