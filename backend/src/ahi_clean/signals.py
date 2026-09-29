"""Scoring primitives derived entirely from the data.

Nothing here knows any field name. Every function takes cells and returns a number
between 0 and 1, and the detectors in `geometry`, `header` and `rowclass` are
built by combining them.
"""

from __future__ import annotations

from collections import Counter

from .typing_utils import EMPTY, infer_type, is_blank


def populated(cells) -> list:
    """The non-blank cells of a line."""
    return [cell for cell in cells if not is_blank(cell)]


def fill_count(cells) -> int:
    return len(populated(cells))


def is_merged_banner(cells) -> bool:
    """Whether a row is one value stretched across several columns.

    openpyxl reports a merged range as its value repeated in every covered cell, so a
    merged title spanning the sheet arrives looking as densely filled as a real record.
    Judged by repetition rather than by the merge list, this also catches a title that
    was copied across cells instead of merged.
    """
    values = populated(cells)
    return len(values) >= 2 and len({str(value).strip() for value in values}) == 1


def effective_fill(cells) -> int:
    """Populated-cell count, with a merged banner counted as the one value it holds."""
    return 1 if is_merged_banner(cells) else fill_count(cells)


# Beyond this many rows, whole-block scans are replaced by a sample. Every question
# these scans answer -- what type does this column hold, how full is a typical row -- is
# a property of the column, and a column of a million values does not need its millionth
# to answer it.
LARGE_BLOCK = 200
PROFILE_SAMPLE = 400


def sampled(lines):
    """A bounded, representative slice of a block of rows.

    The head catches the common case; the stride stops a block that changes character
    part way down from being judged on its opening alone.
    """
    if len(lines) <= PROFILE_SAMPLE:
        return lines
    head = PROFILE_SAMPLE // 2
    stride = max(1, (len(lines) - head) // (PROFILE_SAMPLE - head))
    return list(lines[:head]) + list(lines[head::stride])[: PROFILE_SAMPLE - head]


def modal_fill(lines) -> int:
    """The most common populated-cell count across a set of lines.

    This is the baseline a real record is expected to meet. Banners, footers and
    total rows fall far below it, which is how they are found without a keyword list.
    """
    counts = [effective_fill(line) for line in lines if fill_count(line) > 0]
    if not counts:
        return 0
    return Counter(counts).most_common(1)[0][0]


def uniqueness(cells) -> float:
    """Share of populated cells holding a distinct value.

    A header line's labels do not repeat; a line of records almost always does. This
    is what separates File4's row 1 (``South Zone PC`` four times, 0.45) from its
    column 1 (every field name distinct, 1.00).
    """
    values = [str(cell).strip() for cell in populated(cells)]
    if not values:
        return 0.0
    return len(set(values)) / len(values)


def modal_type(cells) -> str:
    """The most common inferred type among a line's populated cells."""
    types = [infer_type(cell) for cell in cells]
    types = [name for name in types if name != EMPTY]
    if not types:
        return EMPTY
    return Counter(types).most_common(1)[0][0]


def type_profile(lines, width: int) -> tuple[str, ...]:
    """The modal type of each column position across a block of rows.

    Two blocks belonging to the same table have matching profiles even when a run of
    blank rows separates them, which is what makes gap length irrelevant.
    """
    profile = []
    for index in range(width):
        column = [row[index] for row in lines if index < len(row)]
        profile.append(modal_type(column))
    return tuple(profile)


def profile_coverage(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """How far two profiles occupy the same columns, ignoring what type they hold.

    Two chunks of one table use the same columns; a banner uses one column of many.
    Kept separate from type agreement because they answer different questions, and
    conflating them makes a one-column banner look identical to the table below it.
    """
    left_columns = {index for index, kind in enumerate(left) if kind != EMPTY}
    right_columns = {index for index, kind in enumerate(right) if kind != EMPTY}
    if not left_columns or not right_columns:
        return 0.0
    return len(left_columns & right_columns) / len(left_columns | right_columns)


def profile_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """Type agreement across the columns both profiles actually populate.

    A position empty on either side is skipped rather than counted as a mismatch. An
    absent value is not contrary evidence: one chunk of a table having no date on its
    only row says nothing about whether it belongs with the chunk below. Counting
    silence as disagreement is how a table gets split at a row that merely has a gap.

    Coverage is checked separately, so skipping here cannot make a narrow block look
    like a wide one.
    """
    pairs = [
        (one, other)
        for one, other in zip(left, right)
        if one != EMPTY and other != EMPTY
    ]
    if not pairs:
        return 0.0
    return sum(1 for one, other in pairs if one == other) / len(pairs)


def textness(cells) -> float:
    """Share of populated cells that are free text rather than a number, date or id."""
    values = populated(cells)
    if not values:
        return 0.0
    return sum(1 for cell in values if infer_type(cell) == "text") / len(values)


def type_contrast(line, below_profile: tuple[str, ...]) -> float:
    """Share of positions where a line's type differs from the column's type below it.

    A header sits on top of its columns while being unlike them -- text labels above
    numbers, dates and identifiers. This is the strongest schema-free header signal.
    """
    pairs = [
        (infer_type(line[index]), below_profile[index])
        for index in range(min(len(line), len(below_profile)))
        if not is_blank(line[index]) and below_profile[index] != EMPTY
    ]
    if not pairs:
        return 0.0
    return sum(1 for one, other in pairs if one != other) / len(pairs)


def brevity(cells, limit: int = 30) -> float:
    """1.0 when a line's values are short enough to be labels rather than prose."""
    values = populated(cells)
    if not values:
        return 0.0
    mean_length = sum(len(str(cell)) for cell in values) / len(values)
    return 1.0 if mean_length <= limit else 0.5


def emphasis(styles) -> float:
    """Share of populated cells carrying visual emphasis (bold, fill or border).

    Report writers style header rows and leave data rows plain. The signal is free --
    it is already in the file -- and it is additive, so a workbook with no formatting
    at all simply scores 0 here and is decided by the other signals.
    """
    flags = [style for style in styles if style is not None]
    if not flags:
        return 0.0
    return sum(1 for style in flags if style.get("emphasised")) / len(flags)
