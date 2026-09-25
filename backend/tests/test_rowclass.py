"""Row classification by sparsity and arithmetic, with no reliance on keywords."""

from ahi_clean import rowclass
from ahi_clean.rowclass import classify_rows

HEADER = ["Centre", "Producer", "Premium", "Policy"]


def record(index, premium):
    return [f"Zone {index}", "Metro Agency", premium, f"POL-{index:04d}"]


def kinds(verdicts):
    return [verdict.classification for verdict in verdicts]


# --------------------------------------------------------------------------- #
# Sparsity
# --------------------------------------------------------------------------- #


def test_full_rows_are_data():
    body = [record(index, 100.0) for index in range(5)]
    assert kinds(classify_rows(body)) == [rowclass.DATA] * 5


def test_a_single_cell_text_row_is_a_footer():
    body = [record(index, 100.0) for index in range(4)]
    body.append(["*** End of Report ***", None, None, None])
    assert kinds(classify_rows(body))[-1] == rowclass.FOOTER


def test_footer_detection_needs_no_english():
    """Sparsity finds it; the text is never consulted to make the decision."""
    body = [record(index, 100.0) for index in range(4)]
    body.append(["Fин отчёта", None, None, None])
    assert kinds(classify_rows(body))[-1] == rowclass.FOOTER


def test_a_repeated_header_row_is_dropped():
    body = [record(0, 100.0), record(1, 200.0), list(HEADER), record(2, 300.0)]
    verdicts = classify_rows(body, header_row=HEADER)
    assert kinds(verdicts)[2] == rowclass.REPEATED_HEADER


# --------------------------------------------------------------------------- #
# Arithmetic
# --------------------------------------------------------------------------- #


def test_a_total_is_confirmed_by_the_sum_above_it():
    body = [record(0, 100.0), record(1, 200.0), record(2, 300.0)]
    body.append(["Subtotal", None, 600.0, None])
    verdicts = classify_rows(body)
    # It covers every data row, so it is a grand total rather than a subtotal.
    assert kinds(verdicts)[-1] == rowclass.GRAND_TOTAL
    assert verdicts[-1].dropped is True
    assert "sum of all 3 data rows above" in verdicts[-1].reason


def test_total_detection_needs_no_english():
    """The arithmetic decides; the German label is never consulted."""
    body = [record(0, 100.0), record(1, 200.0)]
    body.append(["Zwischensumme", None, 300.0, None])
    assert classify_rows(body)[-1].dropped is True


def test_mid_table_subtotals_are_each_confirmed():
    body = [record(0, 100.0), record(1, 200.0), ["Subtotal A", None, 300.0, None],
            record(2, 400.0), record(3, 500.0), ["Subtotal B", None, 900.0, None]]
    assert kinds(classify_rows(body)) == [
        rowclass.DATA, rowclass.DATA, rowclass.SUBTOTAL,
        rowclass.DATA, rowclass.DATA, rowclass.SUBTOTAL,
    ]


def test_a_grand_total_over_all_data_is_confirmed():
    body = [record(index, 100.0) for index in range(4)]
    body.append(["Grand Total", None, 400.0, None])
    verdicts = classify_rows(body)
    assert kinds(verdicts)[-1] == rowclass.GRAND_TOTAL
    assert "all 4 data rows above" in verdicts[-1].reason


def test_nested_subtotals_and_a_grand_total_that_sums_them():
    """The grand total double-counts the data rows, so it must match the subtotals."""
    body = [
        record(0, 100.0), record(1, 200.0), ["Subtotal A", None, 300.0, None],
        record(2, 400.0), record(3, 500.0), ["Subtotal B", None, 900.0, None],
        ["Grand Total", None, 1200.0, None],
    ]
    verdicts = classify_rows(body)
    assert kinds(verdicts)[2] == rowclass.SUBTOTAL
    assert kinds(verdicts)[5] == rowclass.SUBTOTAL
    assert kinds(verdicts)[6] == rowclass.GRAND_TOTAL
    # 1200 equals both the four data rows and the two subtotals -- either explanation
    # is arithmetically true, so the reason only has to name one of them.
    assert "sum of" in verdicts[6].reason


# --------------------------------------------------------------------------- #
# The conflict rule: never lose a real record
# --------------------------------------------------------------------------- #


def test_a_sparse_genuine_record_is_kept_not_dropped():
    body = [record(index, 100.0) for index in range(4)]
    # A real policy that happens to be missing its producer and centre.
    body.append([None, None, 55.5, "POL-9999"])
    verdicts = classify_rows(body)
    assert verdicts[-1].classification == rowclass.SPARSE_KEPT
    assert verdicts[-1].dropped is False
    assert verdicts[-1].confidence < 1.0
    assert "kept as a partially-filled record" in verdicts[-1].reason


def test_a_sparse_row_labelled_total_whose_figure_is_wrong_is_dropped_and_flagged():
    """Real reports carry stale totals; keeping one as data corrupts every sum.

    The row is already sparse on structure alone -- the wording only decides which
    kind of non-record it is, and it lands in its own class with a review flag rather
    than being passed off as an arithmetically proven total.
    """
    body = [record(index, 100.0) for index in range(4)]
    body.append(["Total Risk Centre", None, 12.34, None])
    verdicts = classify_rows(body)
    assert verdicts[-1].classification == rowclass.UNVERIFIED_TOTAL
    assert verdicts[-1].dropped is True
    assert verdicts[-1].confidence < 1.0
    assert "does not reconcile" in verdicts[-1].reason


def test_wording_alone_cannot_drop_a_full_row():
    """The row must be sparse first. A full record named 'Total ...' stays."""
    body = [record(index, 100.0) for index in range(3)]
    body.append(["Total Risk PC", "Metro Agency", 12.34, "POL-0099"])
    assert classify_rows(body)[-1].classification == rowclass.DATA


def test_a_full_row_named_total_is_plain_data():
    """'Total Risk PC' is a profit centre, not a total. It is not sparse, so it stays."""
    body = [record(index, 100.0) for index in range(3)]
    body.append(["Total Risk PC", "Metro Agency", 400.0, "POL-0099"])
    verdicts = classify_rows(body)
    assert verdicts[-1].classification == rowclass.DATA
    assert verdicts[-1].dropped is False


def test_text_alone_never_drops_a_row():
    body = [["Subtotal of nothing", "Metro Agency", 100.0, "POL-1"]] * 1 + [
        record(index, 100.0) for index in range(3)
    ]
    assert all(not verdict.dropped for verdict in classify_rows(body))


# --------------------------------------------------------------------------- #
# Edges
# --------------------------------------------------------------------------- #


def test_empty_body_is_handled():
    assert classify_rows([]) == []


def test_a_subtotal_over_a_single_row_section_is_proven_not_guessed():
    """A section of one record still has a subtotal, and it is arithmetic, not wording."""
    body = [record(0, 100.0), ["Total", None, 100.0, None]]
    verdicts = classify_rows(body)
    assert verdicts[-1].classification == rowclass.GRAND_TOTAL
    assert "equals the sum of" in verdicts[-1].reason


def wide_record(index):
    """A record with enough non-text columns for type contrast to mean something."""
    return [
        f"Zone {index}", "Metro Agency", 100.5 + index,
        f"POL-{index:04d}", f"2026-01-{index + 1:02d}", 5.5 + index,
    ]


def test_a_header_restated_in_different_words_is_dropped():
    """'PC Number' for 'ProfitCenterNumber' cannot be caught by comparing labels."""
    body = [wide_record(index) for index in range(3)]
    body.append(["Profit Centre", "Producer", "Premium ($)", "Policy No.", "Eff Dt", "Comm %"])
    body += [wide_record(index) for index in range(3, 6)]
    verdicts = classify_rows(body)
    assert verdicts[3].classification == rowclass.REPEATED_HEADER
    assert "text where its columns hold" in verdicts[3].reason


def test_a_genuine_record_is_never_mistaken_for_a_restated_header():
    """The guard that makes the rule safe: records are not text in numeric columns."""
    body = [wide_record(index) for index in range(6)]
    assert all(verdict.classification == rowclass.DATA for verdict in classify_rows(body))


def test_a_partial_header_fragment_is_dropped():
    header = ["Centre", "Producer", "Premium", "Policy"]
    body = [record(index, 100.0) for index in range(3)]
    body.append([None, None, "Premium", "Policy"])
    verdicts = classify_rows(body, header_row=header)
    assert verdicts[-1].classification == rowclass.REPEATED_HEADER
    assert "repeat the header's labels in place" in verdicts[-1].reason
