"""Fine-grained cell type inference.

A coarse ``{str, num, date}`` lattice is nearly useless for deciding table
orientation: business data is mostly strings, so rows and columns both look
string-homogeneous and the signal collapses. Splitting strings by *shape* --
POL-1000001 and PC0001 are ``id_string``, '2026-01-01' is ``date_string`` --
restores the contrast that homogeneity scoring depends on.
"""

from __future__ import annotations

import datetime as _dt
import re

EMPTY = "empty"
BOOL = "bool"
INT = "int"
FLOAT = "float"
DATE = "date"
DATETIME = "datetime"
DATE_STRING = "date_string"
ID_STRING = "id_string"
TEXT = "text"

_ID_PATTERN = re.compile(r"^[A-Za-z]{1,5}[-_]?\d{2,}$")
_DATE_STRING_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?$")
_NUMERIC_PATTERN = re.compile(r"^-?[\d,]*\.?\d+$")


def is_blank(value) -> bool:
    """True for None and for strings that are empty once trimmed."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def infer_type(value) -> str:
    """Classify a single cell value onto the fine-grained lattice."""
    if is_blank(value):
        return EMPTY
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, _dt.datetime):
        # openpyxl hands back datetimes even for date-only cells.
        return DATE if (value.hour, value.minute, value.second) == (0, 0, 0) else DATETIME
    if isinstance(value, _dt.date):
        return DATE
    if isinstance(value, int):
        return INT
    if isinstance(value, float):
        return INT if value.is_integer() else FLOAT

    text = str(value).strip()
    if _DATE_STRING_PATTERN.match(text):
        return DATE_STRING
    if _ID_PATTERN.match(text):
        return ID_STRING
    if _NUMERIC_PATTERN.match(text):
        return INT if "." not in text else FLOAT
    return TEXT


def homogeneity(values) -> float:
    """Share of non-empty cells holding the single most common type.

    1.0 means every populated cell in the sequence is the same type; a sequence
    with nothing in it scores 0.0 so that empty axes never win a vote.
    """
    types = [infer_type(value) for value in values]
    populated = [type_name for type_name in types if type_name != EMPTY]
    if not populated:
        return 0.0
    counts: dict[str, int] = {}
    for type_name in populated:
        counts[type_name] = counts.get(type_name, 0) + 1
    return max(counts.values()) / len(populated)


def mean_homogeneity(sequences) -> float:
    """Mean homogeneity across a set of rows or columns, ignoring empty ones."""
    scores = [homogeneity(sequence) for sequence in sequences]
    scores = [score for score in scores if score > 0.0]
    return sum(scores) / len(scores) if scores else 0.0
