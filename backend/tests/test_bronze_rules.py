"""The AHI File -> Bronze scenario document, rule by rule, without a database.

Behaviour tests: every case is built in memory, so they pin the rules themselves.
"""

from datetime import date

import polars as pl
import pytest

from ahi_bronze import naming, periods, planner
from ahi_bronze.periods import Period
from ahi_bronze.planner import Candidate, Ingested, TableState
from ahi_bronze.schema_compare import DIFFERENT, EVOLVED, IDENTICAL, REORDERED, compare

PATTERN = r"(?:^|[_\-\s.])([a-z]{1,6}[_\-]?\d{2,6})(?=$|[_\-\s.])"
COLS = ["profitcentername", "profitcenternumber", "premium", "accountingeffectivedate"]
H1 = Period("2025-01", "2025-06")
JULY = Period("2025-07", "2025-07")
JAN_JUL = Period("2025-01", "2025-07")


def candidate(key="a", file="ARR_pc0515.xlsx", sha="sha-a", src="pc0515", sheets=("ARR",),
              cols=COLS, rows=10, period=H1, **overrides):
    return Candidate(key, file, sha, src, list(sheets), list(cols), rows, period, **overrides)


def only(items):
    assert len(items) == 1
    return items[0]


# --- naming -------------------------------------------------------------------


@pytest.mark.parametrize("filename, expected", [
    ("ARR_pc0515.xlsx", "pc0515"),
    ("ARR report - PC0094.xlsx", "pc0094"),
    ("pc_002_multisheet.xlsx", "pc002"),
    ("Q4_customer_data.xlsx", None),
    ("premium_2025.xlsx", None),
])
def test_source_system_comes_from_the_file_name_suffix(filename, expected):
    assert naming.source_system_from_filename(filename, PATTERN) == expected


@pytest.mark.parametrize("sheets, expected", [
    (["ARR"], "ext_pc0515_arr"),
    (["Jan"], "ext_pc0515_data"),
    (["June 2025"], "ext_pc0515_data"),
    (["2025-07"], "ext_pc0515_data"),
    (["Jan", "Feb"], "ext_pc0515_data"),  # appended month sheets
    (["ARR_pc0515"], "ext_pc0515_arr"),  # a CSV's sheet is its file name
    (["Market Share"], "ext_pc0515_market_share"),  # "mar" inside a word is not March
])
def test_table_names_follow_the_convention(sheets, expected):
    assert naming.table_name("pc0515", sheets) == expected


def test_long_names_fit_postgres_identifiers_without_colliding():
    a = naming.identifier("ext_pc0515_" + "a" * 80)
    b = naming.identifier("ext_pc0515_" + "a" * 79 + "b")
    assert len(a) <= 63 and len(b) <= 63 and a != b


def test_column_names_are_unique_identifiers():
    assert naming.column_names(["premium", "premium", "Total $", ""]) == ["premium", "premium_2", "total", "column_4"]


# --- schema comparison: the four July cases ------------------------------------


def test_case_1_identical():
    assert compare(COLS, COLS).kind == IDENTICAL


def test_case_2_same_columns_other_order():
    assert compare(COLS, list(reversed(COLS))).kind == REORDERED


def test_case_3_same_names_and_order_different_count():
    more = compare(COLS, COLS + ["commission"])
    fewer = compare(COLS, COLS[:-1])
    assert (more.kind, more.added) == (EVOLVED, ["commission"])
    assert (fewer.kind, fewer.missing) == (EVOLVED, ["accountingeffectivedate"])


def test_case_4_different_schema():
    assert compare(COLS, ["a", "b", "c"]).kind == DIFFERENT


# --- periods ------------------------------------------------------------------


def test_period_comes_from_the_accounting_date_before_other_dates():
    frame = pl.DataFrame({
        "policyeffectivedate": [date(2023, 3, 1), date(2025, 9, 1)],
        "accountingeffectivedate": [date(2025, 1, 15), date(2025, 6, 30)],
    })
    guess = periods.detect(frame, ["ARR"])
    assert guess.period == H1
    assert guess.source == "column accountingeffectivedate"
    assert len(guess.candidates) == 2


def test_month_sheets_take_their_year_from_the_data():
    frame = pl.DataFrame({"premium": [1.0], "postingdate": [date(2025, 2, 3)]})
    guess = periods.detect(frame.drop("postingdate").with_columns(d=pl.lit(date(2025, 2, 3))), ["Jan", "Feb", "Mar"])
    assert guess.period == Period("2025-01", "2025-03")


def test_no_dates_and_no_month_sheets_means_no_period():
    assert periods.detect(pl.DataFrame({"premium": [1.0]}), ["ARR"]).period is None


def test_overlap_and_cover():
    assert H1.overlaps(JAN_JUL) and JAN_JUL.covers(H1) and not H1.overlaps(JULY)


# --- planner: the scenario matrix ----------------------------------------------


def test_single_file_single_sheet_creates_one_table():
    item = only(planner.plan([candidate()], [], []))
    assert (item.action, item.table_name) == (planner.CREATE, "ext_pc0515_arr")
    assert not item.requires_confirmation


def test_multiple_files_same_schema_share_one_table():
    items = planner.plan([
        candidate("a", file="East_pc0515.xlsx", sha="1", sheets=["Jan"]),
        candidate("b", file="West_pc0515.xlsx", sha="2", sheets=["Jan"], period=JULY),
    ], [], [])
    assert [i.action for i in items] == [planner.CREATE, planner.APPEND]
    assert {i.table_name for i in items} == {"ext_pc0515_data"}


def test_multiple_files_different_schemas_get_one_table_each():
    items = planner.plan([
        candidate("a", sha="1", sheets=["Jan"]),
        candidate("b", sha="2", sheets=["Jan"], cols=["x", "y"], period=JULY),
    ], [], [])
    assert [i.action for i in items] == [planner.CREATE, planner.NEW_TABLE]
    assert items[1].table_name == "ext_pc0515_data_2025_07"
    assert not items[1].requires_confirmation  # nothing existing is touched


def _loaded(cols=COLS, period=H1):
    return [TableState("ext_pc0515_arr", list(cols))], [
        Ingested("ing-1", "ext_pc0515_arr", "ARR_pc0515_v1.xlsx", "old", period)
    ]


def test_july_identical_appends():
    tables, history = _loaded()
    item = only(planner.plan([candidate(period=JULY)], tables, history))
    assert item.action == planner.APPEND and not item.requires_confirmation


def test_july_reordered_reorders_then_appends():
    tables, history = _loaded()
    item = only(planner.plan([candidate(period=JULY, cols=list(reversed(COLS)))], tables, history))
    assert item.action == planner.REORDER
    assert item.columns_after == COLS  # the table's order is kept


def test_july_extra_column_evolves_and_needs_confirmation():
    tables, history = _loaded()
    item = only(planner.plan([candidate(period=JULY, cols=COLS + ["commission"])], tables, history))
    assert item.action == planner.EVOLVE and item.requires_confirmation
    assert item.columns_after == COLS + ["commission"]


def test_july_different_schema_goes_to_its_own_table():
    tables, history = _loaded()
    item = only(planner.plan([candidate(period=JULY, cols=["a", "b"])], tables, history))
    assert item.action == planner.NEW_TABLE and item.requires_confirmation
    assert item.table_name == "ext_pc0515_arr_2025_07"


def test_revised_jan_jun_file_replaces_after_confirmation():
    tables, history = _loaded()
    item = only(planner.plan([candidate(sha="revised")], tables, history))
    assert item.action == planner.REPLACE and item.rebuild and item.requires_confirmation
    assert [r["id"] for r in item.replaces] == ["ing-1"]


def test_jan_jul_file_replaces_jan_jun():
    tables, history = _loaded()
    item = only(planner.plan([candidate(sha="jan-jul", period=JAN_JUL)], tables, history))
    assert item.action == planner.REPLACE and [r["id"] for r in item.replaces] == ["ing-1"]


def test_the_same_file_again_is_skipped_unless_reingested():
    tables, history = _loaded()
    skipped = only(planner.plan([candidate(sha="old", period=JULY)], tables, history))
    assert skipped.action == planner.SKIP
    forced = only(planner.plan([candidate(sha="old", period=JULY, action_override=planner.REPLACE)], tables, history))
    assert forced.action == planner.REPLACE and forced.requires_confirmation


def test_reviewer_can_keep_the_old_load_and_append_instead_of_replacing():
    tables, history = _loaded()
    item = only(planner.plan([candidate(sha="new", action_override=planner.APPEND)], tables, history))
    assert item.action == planner.APPEND and item.replaces == [] and item.requires_confirmation


def test_reviewer_table_name_is_used():
    item = only(planner.plan([candidate(table_override="ext_pc0515_arrears")], [], []))
    assert item.table_name == "ext_pc0515_arrears"


def test_missing_source_or_period_blocks_approval():
    item = only(planner.plan([candidate(src=None, period=None)], [], []))
    assert len(item.blockers) == 2
    assert planner.approvable([item], set())


def test_risky_items_need_explicit_confirmation():
    tables, history = _loaded()
    item = only(planner.plan([candidate(sha="revised")], tables, history))
    assert planner.approvable([item], set())
    assert planner.approvable([item], {item.key}) == []


# --- duplicates: regressions ----------------------------------------------------


def test_each_output_of_a_multi_table_file_matches_its_own_earlier_load():
    history = [
        Ingested("i-arr", "ext_pc0515_arr", "Book_pc0515.xlsx", "same", H1, ("ARR",)),
        Ingested("i-dim", "ext_pc0515_agents", "Book_pc0515.xlsx", "same", H1, ("Agents",)),
    ]
    tables = [TableState("ext_pc0515_arr", COLS), TableState("ext_pc0515_agents", ["agent"])]
    item = only(planner.plan([candidate(sha="same", sheets=["Agents"], cols=["agent"])], tables, history))
    assert item.action == planner.SKIP and item.table_name == "ext_pc0515_agents"
    assert [ref["id"] for ref in item.replaces] == ["i-dim"]


def test_a_file_resent_after_going_to_a_separate_table_is_still_a_duplicate():
    history = [Ingested("i-oct", "ext_pc0515_arr_2025_10", "ARR_oct_pc0515.xlsx", "oct", JULY, ("ARR",))]
    tables = [TableState("ext_pc0515_arr", COLS), TableState("ext_pc0515_arr_2025_10", ["a", "b"])]
    item = only(planner.plan([candidate(sha="oct", cols=["a", "b"], period=JULY)], tables, history))
    assert item.action == planner.SKIP and item.table_name == "ext_pc0515_arr_2025_10"


def test_the_same_file_twice_in_one_batch_is_loaded_once():
    items = planner.plan([candidate("a", sha="twin"), candidate("b", sha="twin")], [], [])
    assert [item.action for item in items] == [planner.CREATE, planner.SKIP]
