"""Find the header row of a region, and name its columns.

No alias dictionary and no list of expected field names. A header is recognised by how
it *behaves* relative to the rows beneath it: its cells are of a different type than the
columns they sit above, they are text, they do not repeat, and they are short.

The module is equally responsible for the case where there is no header at all. Naming
a data row as the header is worse than admitting none was found, so a region whose best
score falls below the threshold is reported headerless and given positional names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import signals
from .typing_utils import is_blank

SEARCH_DEPTH = 20
HEADER_THRESHOLD = 0.55
SECOND_ROW_THRESHOLD = 0.55
SECOND_ROW_CONTRAST = 0.5
EMPHASIS_BONUS = 0.15

# Content signals sum to 1.0 so that a file with no formatting is still fully scored.
# Emphasis is added on top as a bonus, never as a required component.
WEIGHTS = {
    "contrast": 0.30,
    "textness": 0.25,
    "uniqueness": 0.20,
    "fill_ratio": 0.15,
    "brevity": 0.10,
}


@dataclass
class HeaderResult:
    row_index: int | None
    names: list[str]
    score: float
    detected: bool
    scores: list[dict] = field(default_factory=list)
    multi_row: bool = False
    notes: list[str] = field(default_factory=list)


def score_row(rows, index: int, width: int, styles=None, context=None) -> tuple[float, dict]:
    """Score how much row ``index`` behaves like a header for the rows beneath it.

    ``context`` supplies the type profile and modal fill of the rows below. Passing it
    in matters at scale: computing them per candidate meant re-scanning the whole block
    once for every row in the search band, which on a large sheet called the type
    classifier some twenty-seven times per cell.
    """
    row = rows[index]
    below = rows[index + 1 :]
    present = signals.populated(row)
    if not below or not present:
        return 0.0, {}

    if context is None:
        profile = signals.type_profile(below, width)
        modal = signals.modal_fill(below)
    else:
        profile, modal = context

    components = {
        "contrast": signals.type_contrast(row, profile),
        "textness": signals.textness(row),
        "uniqueness": signals.uniqueness(row),
        "fill_ratio": min(len(present) / modal, 1.0) if modal else 0.0,
        "brevity": signals.brevity(row),
    }
    score = sum(WEIGHTS[name] * value for name, value in components.items())

    if styles is not None and index < len(styles):
        emphasis = signals.emphasis(styles[index])
        components["emphasis"] = emphasis
        score = min(score + EMPHASIS_BONUS * emphasis, 1.0)

    return score, {name: round(value, 2) for name, value in components.items()}


def find_header(rows, width: int, styles=None) -> HeaderResult:
    """Locate the header row, or report that the region has none."""
    scores = []
    best_index, best_score, best_parts = None, -1.0, {}
    context = _below_context(rows, width)

    for index in range(min(len(rows) - 1, SEARCH_DEPTH)):
        score, parts = score_row(rows, index, width, styles, context)
        scores.append({"row": index, "score": round(score, 3), **parts})
        # Strictly greater keeps the topmost row when two score alike, which is where a
        # real header sits if a data row happens to look label-like.
        if score > best_score:
            best_index, best_score, best_parts = index, score, parts

    if best_score < HEADER_THRESHOLD or best_index is None:
        names = positional_names(width)
        return HeaderResult(
            row_index=None,
            names=names,
            score=round(max(best_score, 0.0), 3),
            detected=False,
            scores=scores,
            notes=[
                f"no row scored above {HEADER_THRESHOLD}; columns named positionally"
            ],
        )

    labels = list(rows[best_index])
    multi_row = False
    notes: list[str] = []

    # A second label row directly beneath the first -- units, sub-categories -- is joined
    # rather than treated as data. Capped at two: deeper hierarchies are flagged, never
    # guessed at.
    if best_index + 2 < len(rows):
        second_score, second_parts = score_row(rows, best_index + 1, width, styles, context)
        # Score alone is not enough to call a row a second header. In a mostly-text
        # table an ordinary record scores around 0.55 on uniqueness and fill alone,
        # which would swallow the first record of every such file. A real second
        # header row also *contrasts* with the data beneath it -- labels above numbers
        # and dates -- while a data row looks exactly like its neighbours.
        if (
            second_score >= SECOND_ROW_THRESHOLD
            and second_parts.get("contrast", 0.0) >= SECOND_ROW_CONTRAST
        ):
            labels = _merge_label_rows(labels, rows[best_index + 1])
            multi_row = True
            notes.append("two-row header merged")

    return HeaderResult(
        row_index=best_index,
        names=build_names(labels),
        score=round(best_score, 3),
        detected=True,
        scores=scores,
        multi_row=multi_row,
        notes=notes,
    )


def _below_context(rows, width):
    """The type profile and modal fill of the body, computed once for the whole search.

    Only used when the block is large enough for the difference between "rows below 3"
    and "rows below 5" to be immaterial. Small blocks keep the exact per-candidate
    computation, because there the distinction can genuinely change the answer.
    """
    if len(rows) <= signals.LARGE_BLOCK:
        return None
    body = signals.sampled(rows[SEARCH_DEPTH:] or rows[1:])
    return signals.type_profile(body, width), signals.modal_fill(body)


def _merge_label_rows(top, bottom) -> list:
    merged = []
    for index in range(max(len(top), len(bottom))):
        upper = top[index] if index < len(top) else None
        lower = bottom[index] if index < len(bottom) else None
        parts = [str(part).strip() for part in (upper, lower) if not is_blank(part)]
        merged.append("_".join(parts) if parts else None)
    return merged


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


def normalize_label(label) -> str:
    """Normalize one header label to a SQL-safe column name.

    Returns an empty string when the label carries no usable characters, which is the
    caller's signal to fall back to a positional name.
    """
    if is_blank(label):
        return ""
    text = re.sub(r"[^a-z0-9]+", "_", str(label).casefold()).strip("_")
    if not text:
        return ""
    # A bare number is a legal label in a spreadsheet but not a usable column name.
    if text.isdigit():
        text = f"col_{text}"
    return text


def positional_names(width: int) -> list[str]:
    return [f"column_{index + 1}" for index in range(width)]


def build_names(labels) -> list[str]:
    """Turn a row of raw labels into unique column names.

    Every degenerate case resolves to a usable name rather than an exception or a lost
    column: blanks and empty strings become positional, bare numbers are prefixed, and
    duplicates are suffixed in order of appearance.
    """
    names: list[str] = []
    seen: dict[str, int] = {}

    for index, label in enumerate(labels):
        name = normalize_label(label) or f"column_{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
            # Guard against a file that already contains the suffixed form.
            while name in seen:
                seen[name] = seen.get(name, 1) + 1
                name = f"{name}_{seen[name]}"
        seen[name] = seen.get(name, 1)
        names.append(name)

    return names
