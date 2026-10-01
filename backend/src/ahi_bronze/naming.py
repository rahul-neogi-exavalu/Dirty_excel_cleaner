"""Bronze table names: ``ext_[source_system]_[sheet_name]``.

A sheet named after a month or date (Jan, Feb, June, 2024-07 ...) would bake the
period into the table name, so those tables get the generic ``ext_[source_system]_data``
instead, and the next month's file lands in the same table.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

PREFIX = "ext"
GENERIC = "data"
# Postgres truncates identifiers longer than this.
MAX_IDENTIFIER = 63

_MONTH = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
# Each alternative must stand on its own: "mar" in "market" is not March.
_PERIOD_TOKEN = re.compile(
    rf"(?<![a-z])(?:{_MONTH})(?![a-z])"
    r"|(?<!\d)(?:19|20)\d{2}[-_/. ]?(?:0?[1-9]|1[0-2])(?!\d)"  # 2024-07, 202407
    r"|(?<!\d)(?:0?[1-9]|1[0-2])[-_/. ](?:19|20)\d{2}(?!\d)"  # 07-2024
    r"|(?<!\d)\d{1,2}[-_/.]\d{1,2}[-_/.]\d{2,4}(?!\d)"  # 07/31/2024
    r"|(?<![a-z])q[1-4](?![a-z\d])"
    r"|(?<![a-z])fy\s?\d{2,4}(?!\d)"
    r"|(?<!\d)(?:19|20)\d{2}(?!\d)",  # a bare year
    re.IGNORECASE,
)


def has_period(text: str) -> bool:
    """Whether a sheet (or file) name carries a month, date, quarter or year."""
    return bool(_PERIOD_TOKEN.search(text or ""))


def slug(text: str) -> str:
    """Lower-case snake_case, safe as an unquoted SQL identifier fragment."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").casefold()).strip("_")


def source_system_from_filename(filename: str, pattern: str) -> str | None:
    """The source system the team suffixed to the file name, e.g. ``ARR_pc0515`` -> ``pc0515``.

    The last match in the stem wins, so ``pc_002_multisheet`` still yields ``pc002``.
    Separators inside the code are dropped so ``PC-0515`` and ``pc0515`` agree.
    """
    stem = Path(filename or "").stem.casefold()
    matches = re.findall(pattern, stem, flags=re.IGNORECASE)
    if not matches:
        return None
    found = matches[-1]
    if isinstance(found, tuple):
        found = next((part for part in found if part), "")
    code = slug(found).replace("_", "")
    return code or None


def clean_source_system(value: str | None) -> str:
    return slug(value or "").replace("_", "")


def table_name(source_system: str, sheet_names: list[str], file_stem: str = "") -> str:
    """The bronze table for one cleaned output.

    * every sheet carries a period, or several sheets were appended -> ``ext_src_data``
    * otherwise the sheet name, minus the source-system code if it repeats it
      (a CSV's only "sheet" is its file name) -> ``ext_src_arr``
    """
    source = clean_source_system(source_system) or "unknown"
    names = [name for name in dict.fromkeys(sheet_names) if name]
    if not names or len(names) > 1 or any(has_period(name) for name in names):
        part = GENERIC
    else:
        part = "_".join(_without_source(slug(names[0]).split("_"), source)) or GENERIC
    return identifier(f"{PREFIX}_{source}_{part}")


def _without_source(tokens: list[str], source: str) -> list[str]:
    """Drop the source code from a name's tokens, whether written ``pc0515`` or ``pc_0515``."""
    kept, index = [], 0
    while index < len(tokens):
        if tokens[index] == source:
            index += 1
        elif index + 1 < len(tokens) and tokens[index] + tokens[index + 1] == source:
            index += 2
        else:
            kept.append(tokens[index])
            index += 1
    return [token for token in kept if token]


def identifier(name: str) -> str:
    """Fit a name in Postgres's identifier limit without two long names colliding."""
    name = slug(name) or "t"
    if name[0].isdigit():
        name = f"t_{name}"
    if len(name) <= MAX_IDENTIFIER:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]
    return f"{name[: MAX_IDENTIFIER - 9].rstrip('_')}_{digest}"


def column_names(names: list[str]) -> list[str]:
    """Column identifiers for the bronze table, unique and within the length limit."""
    used: set[str] = set()
    result = []
    for index, name in enumerate(names):
        base = identifier(name) if slug(name) else f"column_{index + 1}"
        candidate, counter = base, 2
        while candidate in used:
            suffix = f"_{counter}"
            candidate = f"{base[: MAX_IDENTIFIER - len(suffix)]}{suffix}"
            counter += 1
        used.add(candidate)
        result.append(candidate)
    return result


def next_free(name: str, taken: set[str]) -> str:
    """``name``, or ``name_2``, ``name_3`` ... whichever is not taken yet."""
    if name not in taken:
        return name
    counter = 2
    while True:
        suffix = f"_{counter}"
        candidate = f"{name[: MAX_IDENTIFIER - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
        counter += 1
