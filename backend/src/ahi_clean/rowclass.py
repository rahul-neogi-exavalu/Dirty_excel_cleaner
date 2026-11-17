"""Classify a region's body rows without a keyword list or any field names.

Two independent detectors do the work:

* **Sparsity** -- a record fills roughly as many cells as its neighbours. A banner, a
  footer or a total fills far fewer. This finds candidates in any language.
* **Arithmetic** -- a total row is one whose number equals the sum of the rows above it.
  This confirms them, and is what makes the classification language-independent rather
  than merely multilingual.

Text patterns survive only as corroboration. A row is never dropped because of what it
says, and a sparse row that fails the arithmetic check is kept and flagged rather than
discarded -- a partially-filled genuine record is common in real data, and silently
losing one is the worst failure this module could have.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import signals
from .typing_utils import is_blank

SPARSE_RATIO = 0.6
SUM_TOLERANCE = 0.01
# A section can legitimately contain a single record, and its subtotal then equals
# that one row. The candidate must already be sparse to reach the arithmetic at all,
# which is what keeps this from matching an ordinary repeated value.
MIN_SUMMED_ROWS = 1

DATA = "DATA"
BANNER = "BANNER"
FOOTER = "FOOTER"
SUBTOTAL = "SUBTOTAL"
GRAND_TOTAL = "GRAND_TOTAL"
REPEATED_HEADER = "REPEATED_HEADER"
UNVERIFIED_TOTAL = "UNVERIFIED_TOTAL"
SPARSE_KEPT = "SPARSE_KEPT"
# A label alone in the first column over rows that leave that column blank: the sheet's way of
# saying "everything below belongs to this payee". Not a record, but its text is data.
GROUP_LABEL = "GROUP_LABEL"
# A line of the sheet that held nothing but Excel error values (#VALUE!, #REF! ...).
EXCEL_ERROR = "EXCEL_ERROR"

DROPPED = {BANNER, FOOTER, SUBTOTAL, GRAND_TOTAL, REPEATED_HEADER, UNVERIFIED_TOTAL, GROUP_LABEL}

HEADER_CONTRAST = 0.6
HEADER_TEXTNESS = 0.8

# Corroboration only. Never sufficient on its own to drop a row.
_TOTAL_HINT = re.compile(r"\b(total|subtotal|sum|sub-total|gesamt|totale|suma)\b", re.IGNORECASE)
_FOOTER_HINT = re.compile(
    r"(end of report|confidential|for queries|generated on|prepared by|page \d+)", re.IGNORECASE
)


@dataclass
class RowVerdict:
    index: int
    classification: str
    reason: str
    confidence: float = 1.0

    @property
    def dropped(self) -> bool:
        return self.classification in DROPPED


def classify_rows(body, header_names=None, header_row=None) -> list[RowVerdict]:
    """Classify every row of a region's body in document order."""
    if not body:
        return []

    reference = signals.sampled(body)
    modal = signals.modal_fill(reference)
    threshold = max(modal * SPARSE_RATIO, 2) if modal else 0
    width = max((len(row) for row in body), default=0)
    profile = signals.type_profile(reference, width)

    # Normalised once rather than per row: the header does not change between rows.
    header_norm = _norm_cells(header_row) if header_row is not None else None

    verdicts: list[RowVerdict] = []
    for index, row in enumerate(body):
        verdicts.append(
            _initial_verdict(index, row, threshold, header_row, profile, header_norm)
        )

    _confirm_totals(body, verdicts)
    _confirm_dense_total(body, verdicts)
    _resolve_unconfirmed(body, verdicts)
    _mark_group_labels(body, verdicts)
    _keep_interleaved_labels(body, verdicts)
    return verdicts


def _confirm_dense_total(body, verdicts) -> None:
    """Catch a grand total that is *not* sparse: the last data row, if it sums the rest.

    The sparse path never sees a total that is the fullest row on the sheet -- a ledger
    whose records each fill one month while the total fills all twelve. So the last DATA
    row is tested too, under a stricter rule than the sparse path: *every* number in it
    must equal its column's sum over the data rows above (an empty column sums to 0),
    and on top of that either two or more of those numbers are non-zero, or the row
    carries fewer labels than every record above it (a total has a word where a record
    has its names and codes). One matching column alone is not enough: a last record of
    30 under records of 10 and 20 is just a record.

    Records include the sparse rows still pending (a carrier with one month filled in);
    the candidate itself must be a full DATA row, the sparse path having had its turn.
    """
    kept = [v for v in verdicts if v.classification in (DATA, SPARSE_KEPT)]
    if len(kept) < 2 or kept[-1].classification != DATA:
        return
    last, above = kept[-1], [verdict.index for verdict in kept[:-1]]
    row = body[last.index]
    numbers = [(position, _as_number(cell)) for position, cell in enumerate(row)]
    numbers = [(position, value) for position, value in numbers if value is not None]
    if not numbers:
        return

    def label_count(cells) -> int:
        return sum(1 for cell in cells if not is_blank(cell) and _as_number(cell) is None)

    fewer_labels = label_count(row) < min(label_count(body[index]) for index in above)
    matched, missed = 0, 0
    for position, target in numbers:
        column = [_as_number(body[index][position]) if position < len(body[index]) else None
                  for index in above]
        total = sum(value for value in column if value is not None)
        if abs(total - target) <= max(SUM_TOLERANCE, abs(target) * 1e-9):
            matched += target != 0
        else:
            missed += 1
    # Two or more exact column sums settle it, with one column allowed to disagree: a
    # total row carries a ratio (average commission rate) that is not a sum. Otherwise
    # every number must match and the row must read like a total, not a record.
    if not ((matched >= 2 and missed <= 1 and len(above) >= 2)
            or (matched >= 1 and missed == 0 and fewer_labels)):
        return
    last.classification = GRAND_TOTAL
    last.reason = (
        f"every number in the last row equals its column's sum over the {len(above)} "
        "data rows above"
    )


def _keep_interleaved_labels(body, verdicts) -> None:
    """A label-only row *between* records is a record with empty measures, not a footer.

    "Single cell, no numbers" reads as a footer at the foot of a table. Between data rows
    -- a carrier listed with no business this month, sitting in its alphabetical place --
    it is a record whose figures happen to be blank, and dropping it loses the carrier.
    Only when its one value is in the table's first column, and that column holds text.
    """
    data_rows = [verdict.index for verdict in verdicts if verdict.classification == DATA]
    if not data_rows:
        return
    key = min(
        (next((p for p, cell in enumerate(body[i]) if not is_blank(cell)), 0) for i in data_rows),
        default=0,
    )

    def keyed(index) -> bool:
        row = body[index]
        return key < len(row) and isinstance(row[key], str) and not is_blank(row[key])

    # The mirror case: after the last record, a sparse row with no number in it is the
    # report's own footer (its grouping and filter criteria), not a record.
    for verdict in verdicts:
        if (verdict.classification == SPARSE_KEPT and verdict.index > data_rows[-1]
                and not any(_as_number(cell) is not None for cell in body[verdict.index])):
            verdict.classification = FOOTER
            verdict.reason = "sparse text after the last record, no numbers: report footer"

    for verdict in verdicts:
        if verdict.classification != FOOTER:
            continue
        before = [index for index in data_rows if index < verdict.index]
        after = [index for index in data_rows if index > verdict.index]
        # Between two records that both fill the key column -- not above the first
        # record (a note under the header) and not over records that leave it blank
        # (a group label, settled already).
        if not before or not after or not (keyed(before[-1]) and keyed(after[0])):
            continue
        row = body[verdict.index]
        filled = [position for position, cell in enumerate(row) if not is_blank(cell)]
        if filled != [key] or not isinstance(row[key], str) or _FOOTER_HINT.search(row[key]):
            continue
        if _TOTAL_HINT.search(row[key]):
            continue
        verdict.classification = SPARSE_KEPT
        verdict.confidence = 0.5
        verdict.reason = "label-only row between records: kept as a record with empty measures"


def _initial_verdict(index, row, threshold, header_row, profile, header_norm=None) -> RowVerdict:
    fill = signals.fill_count(row)

    if header_norm is not None and _matches_header(row, header_norm):
        return RowVerdict(index, REPEATED_HEADER, "row repeats the header labels")

    if header_row is not None and _repeats_header_labels(row, header_row):
        return RowVerdict(
            index, REPEATED_HEADER, "populated cells repeat the header's labels in place"
        )

    if _reads_as_a_header(row, profile):
        return RowVerdict(
            index,
            REPEATED_HEADER,
            "row is text where its columns hold numbers, dates or ids -- a header in "
            "different words",
        )

    if fill >= threshold:
        return RowVerdict(index, DATA, f"fill {fill} meets the region's baseline")

    lead = _lead_text(row)
    has_number = any(_as_number(cell) is not None for cell in row)

    if fill <= 1 and not has_number:
        kind = FOOTER
        reason = f"single populated cell, no numbers (fill {fill})"
        if _FOOTER_HINT.search(lead):
            reason += "; text corroborates"
        return RowVerdict(index, kind, reason)

    # Sparse and numeric: a total until arithmetic says otherwise.
    return RowVerdict(index, SPARSE_KEPT, f"sparse (fill {fill}) pending arithmetic check")


# Of the data rows, at least this share must leave the first column blank for it to be read as
# a column of group labels rather than a column that is merely patchy.
GROUP_BLANK_SHARE = 0.5


def _mark_group_labels(body, verdicts) -> None:
    """Recognise group labels: one cell, in the first column, heading rows that leave it blank.

    ``Gallagher Benefit`` on a line of its own, then records whose first cell is empty because
    the label above stands for them. Left alone the label is dropped as a footer and the
    records lose the one thing that says whose they are. A label is only recognised when a
    data row follows it with the first column blank, and when most data rows do leave that
    column blank -- a lone line at the bottom of a sheet, or a title over a table whose first
    column is filled, is still just a footer or a banner.
    """
    data = [verdict.index for verdict in verdicts if verdict.classification == DATA]
    if not data:
        return
    blank_first = sum(1 for index in data if is_blank(body[index][0]) if body[index])
    if blank_first / len(data) < GROUP_BLANK_SHARE:
        return
    for verdict in verdicts:
        if verdict.classification not in (FOOTER, BANNER):
            continue
        row = body[verdict.index]
        if signals.fill_count(row) != 1 or is_blank(row[0]):
            continue
        following = verdicts[verdict.index + 1] if verdict.index + 1 < len(verdicts) else None
        if (
            following is not None
            and following.classification == DATA
            and is_blank(body[following.index][0])
        ):
            verdict.classification = GROUP_LABEL
            verdict.reason = (
                "a label alone in the first column over rows that leave it blank; "
                "carried into those rows"
            )


def carry_group_labels(body, verdicts) -> tuple[list, bool]:
    """The body with every group label filled down into the first column beneath it.

    Returns the rows (copies where changed) and whether anything was carried. A row whose
    first cell is filled keeps its own value; only blanks inherit the label above.
    """
    current = None
    carried = False
    out = []
    for verdict, row in zip(verdicts, body):
        if verdict.classification == GROUP_LABEL:
            current = row[0]
        elif current is not None and not verdict.dropped and row and is_blank(row[0]):
            row = [current] + list(row[1:])
            carried = True
        out.append(row)
    return out, carried


def _norm_cells(cells) -> list[str]:
    return [str(cell).strip().casefold() for cell in cells if not is_blank(cell)]


def _matches_header(row, header_norm) -> bool:
    values = _norm_cells(row)
    return bool(values) and values == header_norm


def _repeats_header_labels(row, header_row) -> bool:
    """Whether a partial row echoes the header's own labels at the same positions.

    A section marker like ``Policy Summary | | | | Premium | PolicyNumber`` is a
    fragment of the header, not a record. Checked positionally against the header
    this region already found, so it needs no vocabulary of its own.
    """
    matched = 0
    for index, cell in enumerate(row):
        if is_blank(cell) or index >= len(header_row) or is_blank(header_row[index]):
            continue
        if str(cell).strip().casefold() == str(header_row[index]).strip().casefold():
            matched += 1
        else:
            return False
    return matched >= 2


def _reads_as_a_header(row, profile) -> bool:
    """Whether a row is text sitting where its columns hold something else.

    A section that restates the header in different words -- ``PC Number`` for
    ``ProfitCenterNumber`` -- cannot be caught by comparing labels. It is caught the
    same way the header itself was found: it is text where the column beneath it is
    numeric, dated or an identifier.
    """
    if signals.fill_count(row) < 2:
        return False
    return (
        signals.type_contrast(row, profile) >= HEADER_CONTRAST
        and signals.textness(row) >= HEADER_TEXTNESS
    )


def _lead_text(row) -> str:
    for cell in row:
        if not is_blank(cell):
            return str(cell)
    return ""


def _as_number(cell):
    if isinstance(cell, bool) or is_blank(cell):
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    try:
        return float(str(cell).replace(",", "").replace("%", "").replace("$", "").strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Arithmetic confirmation
# --------------------------------------------------------------------------- #


def _confirm_totals(body, verdicts) -> None:
    """Confirm sparse numeric rows as totals by checking they sum the rows above.

    Subtotals are settled first against the contiguous data run immediately above them.
    Only then are the survivors tested as grand totals -- against every data row above,
    and against the subtotals already confirmed. Doing it in that order is what stops a
    grand total being missed because its figure double-counts rows a subtotal already
    covered.
    """
    # Building the numeric view of every column costs a full pass over the sheet, and
    # it answers a question only sparse rows ask. Most sheets have none.
    if not any(verdict.classification == SPARSE_KEPT for verdict in verdicts):
        return

    width = max((len(row) for row in body), default=0)
    columns = {
        position: [_as_number(row[position]) if position < len(row) else None for row in body]
        for position in range(width)
    }

    # Pass 1 -- subtotals against the contiguous data run directly above.
    for verdict in verdicts:
        if verdict.classification != SPARSE_KEPT:
            continue
        for position, values in columns.items():
            target = values[verdict.index]
            if target is None:
                continue
            run = _contiguous_data_above(verdicts, values, verdict.index)
            if len(run) >= MIN_SUMMED_ROWS and abs(sum(run) - target) <= SUM_TOLERANCE:
                # Scope is what separates the two labels. A run covering every data row
                # in the region is a grand total; one covering a proper subset is a
                # subtotal. Without this they are the same check and the distinction
                # would be arbitrary -- both are dropped either way, but the audit
                # report should say which it was.
                total_data = sum(1 for other in verdicts if other.classification == DATA)
                whole = len(run) == total_data
                verdict.classification = GRAND_TOTAL if whole else SUBTOTAL
                scope = "all" if whole else "the"
                verdict.reason = (
                    f"column {position} equals the sum of {scope} {len(run)} data rows above"
                )
                break

    # Pass 2 -- grand totals against all data above, or against the confirmed subtotals.
    for verdict in verdicts:
        if verdict.classification != SPARSE_KEPT:
            continue
        for position, values in columns.items():
            target = values[verdict.index]
            if target is None:
                continue
            all_data = [
                values[other.index]
                for other in verdicts
                if other.index < verdict.index
                and other.classification == DATA
                and values[other.index] is not None
            ]
            subtotals = [
                values[other.index]
                for other in verdicts
                if other.index < verdict.index
                and other.classification == SUBTOTAL
                and values[other.index] is not None
            ]
            for candidate, label in ((all_data, "data rows"), (subtotals, "subtotals")):
                if len(candidate) >= MIN_SUMMED_ROWS and abs(sum(candidate) - target) <= SUM_TOLERANCE:
                    verdict.classification = GRAND_TOTAL
                    verdict.reason = (
                        f"column {position} equals the sum of all {len(candidate)} {label} above"
                    )
                    break
            if verdict.classification == GRAND_TOTAL:
                break


def _contiguous_data_above(verdicts, values, index) -> list[float]:
    """Values of the unbroken run of data rows immediately above ``index``."""
    run: list[float] = []
    for position in range(index - 1, -1, -1):
        if verdicts[position].classification != DATA:
            break
        value = values[position]
        if value is None:
            break
        run.append(value)
    return run


def _resolve_unconfirmed(body, verdicts) -> None:
    """Decide what to do with sparse rows the arithmetic could not confirm.

    They are kept. A record with several empty fields is ordinary in real data, and the
    cost of wrongly dropping one is far higher than the cost of carrying a flagged row
    that a reviewer can look at.
    """
    for verdict in verdicts:
        if verdict.classification != SPARSE_KEPT:
            continue
        lead = _lead_text(body[verdict.index])
        if _TOTAL_HINT.search(lead):
            # The row is already sparse on structure alone; the wording only decides
            # which kind of non-record it is. Real reports carry totals whose figures
            # are stale, hand-edited or rounded, and keeping those as data corrupts
            # every downstream sum. Dropped, but into its own class and flagged, never
            # conflated with a total the arithmetic actually proved.
            verdict.classification = UNVERIFIED_TOTAL
            verdict.confidence = 0.5
            verdict.reason = (
                f"{verdict.reason}; labelled a total but the figure does not reconcile "
                "with the rows above -- dropped, needs review"
            )
        else:
            verdict.confidence = 0.5
            verdict.reason += "; kept as a partially-filled record"
