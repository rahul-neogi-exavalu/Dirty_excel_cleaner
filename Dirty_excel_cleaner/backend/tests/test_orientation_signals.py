"""The small helpers behind the orientation fixes, pinned on their own."""

from ahi_clean import coerce, pivot, rowclass, signals
from ahi_clean.rowclass import classify_rows


# --- signals ----------------------------------------------------------------------------

def test_modal_fill_goes_to_the_larger_fill_on_a_tie():
    # Two one-cell rows, two three-cell rows: "one" must not win by being seen first.
    lines = [["Title", None, None], ["Name", 1, 2.5], ["Other", None, None], ["More", 3, 4.5]]
    assert signals.modal_fill(lines) == 3
    assert signals.modal_fill(list(reversed(lines))) == 3


def test_a_clear_majority_still_decides_modal_fill():
    assert signals.modal_fill([["a", None], ["b", None], ["c", 1]]) == 1


def test_leading_titles_and_banners_are_stripped_but_not_sparse_rows_further_down():
    banner = ["Report"] * 4          # merged: one value stretched across the row
    title = ["Report", None, None, None]
    table = [["Name", "A", "B", "C"], ["x", 1, 2, 3], ["y", None, None, None], ["z", 4, 5, 6]]
    assert signals.strip_leading_decoration([banner] + table) == table
    assert signals.strip_leading_decoration([title, banner] + table) == table
    # The sparse "y" row is a record with figures missing, not decoration.
    assert signals.strip_leading_decoration(table) == table


def test_a_block_that_is_all_decoration_is_left_alone():
    # Nothing fuller follows, so there is nothing to strip towards: the block comes back whole.
    banner = [["Report"] * 3, ["Report"] * 3]
    assert signals.strip_leading_decoration(banner) == banner


# --- month names ------------------------------------------------------------------------

def test_a_run_of_months_is_recognised_whatever_surrounds_it():
    assert coerce.looks_like_month_names(["Carriers", "AM Best", "Billing Co", "January", "February", "March"])
    assert coerce.looks_like_month_names(["Jan", "Feb", "Mar", "Apr"])
    assert coerce.looks_like_month_names(["JANUARY", None, "February", "March."])


def test_ordinary_headings_are_not_months():
    assert not coerce.looks_like_month_names(["Carriers", "AM Best", "Billing Co"])
    assert not coerce.looks_like_month_names(["Name", "May", "Notes"])  # one month is a name
    assert not coerce.looks_like_month_names(["Jan", "Feb", "Producer", "Policy", "Premium", "Region"])


def test_month_names_do_not_change_what_counts_as_a_pivot_axis():
    # A bare month is still not a point in time for the pivot machinery.
    assert not coerce.looks_like_periods(["January", "February", "March"])


def test_period_axis_looks_in_the_top_rows_only():
    months = ["Carriers", "AM Best", "Billing Co", "January", "February", "March"]
    grid = [["Title"] + [None] * 5, months, ["Munich", "000149", "APEX", None, None, 270090]]
    assert pivot.looks_period_axis(grid)
    assert not pivot.looks_period_axis([["a", "b", "c"]] * 8 + [months])


# --- group labels -----------------------------------------------------------------------

def _record(first, premium, policy):
    return [first, "01/01/2026", "Yes", premium, policy]


def test_a_label_alone_over_rows_with_a_blank_first_cell_is_a_group_label():
    body = [
        ["Gallagher Benefit", None, None, None, None],
        _record(None, 10.0, "GL-1"),
        _record(None, 20.0, "GL-2"),
        ["One80 (Vista)", None, None, None, None],
        _record(None, 30.0, "GL-3"),
    ]
    verdicts = classify_rows(body)
    assert [v.classification for v in verdicts] == [
        rowclass.GROUP_LABEL, rowclass.DATA, rowclass.DATA, rowclass.GROUP_LABEL, rowclass.DATA]
    assert all(v.dropped for v in verdicts if v.classification == rowclass.GROUP_LABEL)


def test_group_labels_are_filled_down_until_the_next_label():
    body = [
        ["Gallagher Benefit", None, None, None, None],
        _record(None, 10.0, "GL-1"),
        _record(None, 20.0, "GL-2"),
        ["One80 (Vista)", None, None, None, None],
        _record(None, 30.0, "GL-3"),
    ]
    filled, carried = rowclass.carry_group_labels(body, classify_rows(body))
    assert carried
    assert [row[0] for row in filled] == ["Gallagher Benefit", "Gallagher Benefit", "Gallagher Benefit",
                                          "One80 (Vista)", "One80 (Vista)"]


def test_a_row_that_has_its_own_first_cell_keeps_it():
    body = [
        ["Group A", None, None, None, None],
        _record(None, 10.0, "GL-1"),
        _record("Own value", 20.0, "GL-2"),
        _record(None, 30.0, "GL-3"),
    ]
    filled, _ = rowclass.carry_group_labels(body, classify_rows(body))
    assert [row[0] for row in filled][1:] == ["Group A", "Own value", "Group A"]


def test_a_footer_under_a_table_whose_first_column_is_filled_is_not_a_group_label():
    body = [_record(f"Payee {index}", 10.0 + index, f"GL-{index}") for index in range(5)]
    body.append(["Report generated for the month", None, None, None, None])
    verdicts = classify_rows(body)
    assert verdicts[-1].classification == rowclass.FOOTER
    assert rowclass.GROUP_LABEL not in {v.classification for v in verdicts}


def test_a_lone_line_at_the_bottom_is_not_a_group_label_even_when_the_first_column_is_blank():
    body = [_record(None, 10.0 + index, f"GL-{index}") for index in range(4)]
    body.append(["Report for: July", None, None, None, None])
    assert classify_rows(body)[-1].classification == rowclass.FOOTER
