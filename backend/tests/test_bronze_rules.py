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
    assert items[1].table_name == "ext_pc0515_data_v2"  # no period in a table name
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
    assert item.table_name == "ext_pc0515_arr_v2"


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


# --- audit fixes ------------------------------------------------------------------

MAY_JUL = Period("2025-05", "2025-07")


def test_a_revised_file_with_another_schema_still_replaces_the_old_load():
    # Jan-Jun and July loaded; a revised Jan-Jun file with new columns goes to a table of
    # its own, and the old Jan-Jun load is replaced -- in the table it is in.
    tables = [TableState("ext_pc0515_arr", COLS)]
    history = [Ingested("ing-1", "ext_pc0515_arr", "ARR_v1_pc0515.xlsx", "old", H1),
               Ingested("ing-2", "ext_pc0515_arr", "ARR_jul_pc0515.xlsx", "jul", JULY)]
    item = only(planner.plan([candidate(sha="revised", cols=["x", "y"])], tables, history))
    assert item.action == planner.NEW_TABLE and not item.rebuild
    assert item.replaces == [{"id": "ing-1", "file_name": "ARR_v1_pc0515.xlsx", "period_start": "2025-01",
                              "period_end": "2025-06", "table_name": "ext_pc0515_arr"}]


def test_a_partial_overlap_waits_for_the_reviewer():
    tables, history = _loaded()
    item = only(planner.plan([candidate(sha="may-jul", period=MAY_JUL)], tables, history))
    assert item.blockers and "only partly" in item.blockers[0]
    chosen = only(planner.plan([candidate(sha="may-jul", period=MAY_JUL, action_override=planner.APPEND)],
                               tables, history))
    assert chosen.action == planner.APPEND and not chosen.blockers


def test_two_tables_on_one_sheet_are_not_the_same_file():
    items = planner.plan([candidate("a", sha="same", regions=("Report",)),
                          candidate("b", sha="same", cols=["agent", "region"], rows=3, regions=("Report_r2",))], [], [])
    # Loaded, not skipped as "the same file": the side table gets a table of its own.
    assert [item.action for item in items] == [planner.CREATE, planner.NEW_TABLE]
    assert len({item.table_name for item in items}) == 2


def test_jan_jun_and_jan_jul_in_one_batch_load_only_jan_jul():
    items = planner.plan([candidate("jul", file="ARR_JanJul_pc0515.xlsx", sha="b", period=JAN_JUL),
                          candidate("jun", file="ARR_JanJun_pc0515.xlsx", sha="a", period=H1)], [], [])
    by_key = {item.key: item for item in items}
    assert by_key["jun"].action == planner.SKIP and "ARR_JanJul_pc0515.xlsx" in by_key["jun"].reasons[-1]
    assert by_key["jul"].action == planner.CREATE


def test_reordered_columns_with_a_new_one_evolve():
    tables, history = _loaded()
    item = only(planner.plan([candidate(period=JULY, cols=list(reversed(COLS)) + ["commission"])], tables, history))
    assert item.action == planner.EVOLVE and item.columns_after == COLS + ["commission"]
    assert item.comparison["reordered"]


def test_the_sheet_column_of_a_stacked_table_is_not_a_schema_change():
    tables = [TableState("ext_pc0515_arr", ["source_sheet"] + COLS, provenance=("source_sheet",))]
    history = [Ingested("ing-1", "ext_pc0515_arr", "ARR_v1_pc0515.xlsx", "old", H1)]
    item = only(planner.plan([candidate(period=JULY)], tables, history))
    assert item.action == planner.APPEND and not item.requires_confirmation


def test_swapped_duplicate_headers_load_into_their_own_columns():
    tables = [TableState("ext_pc0515_arr", ["amount", "amount_2"],
                         headers={"amount": "Amount ($)", "amount_2": "Amount (%)"})]
    history = [Ingested("ing-1", "ext_pc0515_arr", "ARR_v1_pc0515.xlsx", "old", H1)]
    swapped = candidate(period=JULY, cols=["amount", "amount_2"],
                        headers={"amount": "Amount (%)", "amount_2": "Amount ($)"})
    item = only(planner.plan([swapped], tables, history))
    assert item.column_map == {"amount": "amount_2", "amount_2": "amount"}
    # Matched by header, the file simply lists the two the other way round.
    assert item.action == planner.REORDER and item.columns_after == ["amount", "amount_2"]


def test_a_second_report_reuses_its_own_table_next_month():
    tables = [TableState("ext_pc0515_data", COLS), TableState("ext_pc0515_data_v2", ["x", "y"])]
    history = [Ingested("ing-1", "ext_pc0515_data", "A_pc0515.xlsx", "a", H1),
               Ingested("ing-2", "ext_pc0515_data_v2", "B_pc0515.xlsx", "b", H1)]
    item = only(planner.plan([candidate(sha="c", sheets=["Jul"], cols=["x", "y"], period=JULY)], tables, history))
    assert (item.action, item.table_name) == (planner.APPEND, "ext_pc0515_data_v2")


@pytest.mark.parametrize("sheet, periodic", [
    ("June", True), ("Jun-25", True), ("2026-01", True), ("Q1", True), ("FY25", True), ("1H25", True),
    ("Data H1", True), ("Week 32", True), ("SEP Plans", False), ("Dec Adj", False), ("Sheet1", False),
    ("Market Share", False),
])
def test_period_words_in_sheet_names(sheet, periodic):
    assert naming.has_period(sheet) is periodic


def test_a_year_far_from_the_files_own_is_a_name():
    assert naming.table_name("pc0515", ["Plan 2000"], years={2026}) == "ext_pc0515_plan_2000"
    assert naming.table_name("pc0515", ["Plan 2026"], years={2026}) == "ext_pc0515_data"


@pytest.mark.parametrize("filename, expected", [
    ("ARR_pc0515_FY2025.xlsx", "pc0515"),
    ("ARR_pc0515_jun2025.xlsx", "pc0515"),
    ("ARR_pc0515_v12.xlsx", "pc0515"),
    ("arr2026_x.xlsx", None),
])
def test_a_period_or_version_is_not_a_source_system(filename, expected):
    assert naming.source_system_from_filename(filename, PATTERN) == expected


def test_long_identifiers_hash_with_sha256_and_remember_the_old_name():
    long = "ext_pc0515_" + "a" * 80
    assert naming.identifier(long) != naming.legacy_identifier(long)
    old = naming.table_name("pc0515", ["a" * 80], legacy=True)
    item = only(planner.plan([candidate(sheets=["a" * 80], period=JULY)], [TableState(old, COLS)],
                             [Ingested("ing-1", old, "x.xlsx", "x", H1)]))
    assert item.table_name == old and item.action == planner.APPEND
