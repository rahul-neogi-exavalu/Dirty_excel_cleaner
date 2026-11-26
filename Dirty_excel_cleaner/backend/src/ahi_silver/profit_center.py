"""Profit Center Name and Number, populated from the LOTL as the AHI document says.

| Name    | Number  | Result                                                   |
|---------|---------|----------------------------------------------------------|
| present | present | kept; when the LOTL gives another number for the name, the LOTL's wins (corrected) |
| present | missing | number from the LOTL by name (filled_number)             |
| missing | missing | name (legacy_office_name) and number from the LOTL by the file's profit center (filled_name) |

The LOTL is the business's ``pc_name_pc_number_from_lotl`` table: profit_center_number
(unpadded, e.g. ``796``; sub-offices ``069_01``; sometimes the text ``null``),
legacy_office_name and status. When a name or number appears twice, the Active row wins.
Names are matched without case, extra spaces or line breaks, and with every dash read as
'-' ("BSG California – legacy Hull Stockton" is "BSG California - legacy Hull Stockton").
A cell holding the text ``null``, ``none`` or ``n/a`` is missing, in the file as in the LOTL.

Numbers are standardised to four digits (94 -> 0094, 069_01 -> 0069_01). The file's
profit center is the bronze pc_id (PC0796 -> 0796). A row the LOTL cannot settle is kept
as is and flagged, never dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KEPT = "kept"
CORRECTED = "corrected"
FILLED_NUMBER = "filled_number"
FILLED_NAME = "filled_name"
NO_MATCH = "no_match"
NAME_MISSING = "name_missing"
UNAVAILABLE = "lotl_unavailable"

ACTIVE = "active"


def _value(value) -> str | None:
    """Trimmed text; blank and the exported text ``null`` are missing."""
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.casefold() in ("null", "none", "n/a") else text


# Hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar, minus sign.
_DASHES = re.compile("[‐-―−]")


def name_key(name: str) -> str:
    """A legacy office name as compared: case, runs of whitespace and dash styles aside."""
    return " ".join(_DASHES.sub("-", name).split()).casefold()


@dataclass
class Lotl:
    # padded number -> (legacy office name, status)
    by_number: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    # name_key(legacy office name) -> padded number
    by_name: dict[str, str] = field(default_factory=dict)
    rows: int = 0

    @classmethod
    def from_rows(cls, rows) -> "Lotl":
        """``rows``: (profit_center_number, legacy_office_name, status)."""
        lotl = cls()
        active_number: set[str] = set()
        active_name: set[str] = set()
        for number, name, status in rows:
            number, name, status = pad(_value(number)), _value(name), _value(status)
            lotl.rows += 1
            active = (status or "").casefold() == ACTIVE
            # An Active row replaces an Inactive one; never the other way round.
            if number and name and (number not in lotl.by_number or (active and number not in active_number)):
                lotl.by_number[number] = (name, status)
                if active:
                    active_number.add(number)
            key = name_key(name) if name else None
            if key and number and (key not in lotl.by_name or (active and key not in active_name)):
                lotl.by_name[key] = number
                if active:
                    active_name.add(key)
        return lotl

    @property
    def empty(self) -> bool:
        return not self.by_number and not self.by_name


def pc_id(source_system: str | None) -> str | None:
    """Only the numeric portion of a source system or pc_id, as a string (pc0515 -> 0515)."""
    digits = re.sub(r"\D", "", source_system or "")
    return digits or None


def pc_number(pc: str | None) -> str | None:
    """The padded profit-center number of a pc_id: PC0796 -> 0796, PC069_01 -> 0069_01."""
    if not pc:
        return None
    return pad(re.sub(r"^\s*pc[\s_\-]?", "", str(pc), flags=re.IGNORECASE))


def pad(value) -> str | None:
    """Four-digit profit center number: 94 -> 0094, '94.0' -> 0094, 069_01 -> 0069_01;
    other text kept."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+)(?:\.0+)?(?:_(\d+))?", text)
    if not match:
        return text
    number = match.group(1).lstrip("0").zfill(4) if len(match.group(1)) <= 4 else match.group(1)
    return f"{number}_{match.group(2)}" if match.group(2) else number


def _number(value) -> str | None:
    """A row's profit center number, padded; 'PC0094' is read as 0094."""
    text = _value(value)
    if text and re.match(r"^\s*pc[\s_\-]?\d", text, flags=re.IGNORECASE):
        return pc_number(text)
    return pad(text)


def resolve(name, number, pc: str | None, lotl: Lotl) -> tuple[str | None, str | None, str]:
    """(name, number, status) for one row. ``pc``: the row's load's pc_id (PC0796)."""
    name = _value(name)
    number = _number(number)
    if lotl.empty:
        return name, number, UNAVAILABLE
    if name and number:
        expected = lotl.by_name.get(name_key(name))
        if expected is None:
            return name, number, NO_MATCH
        return (name, number, KEPT) if expected == number else (name, expected, CORRECTED)
    if name:
        expected = lotl.by_name.get(name_key(name))
        return (name, expected, FILLED_NUMBER) if expected else (name, None, NO_MATCH)
    if number:
        return None, number, NAME_MISSING  # not a case the document covers: flagged, kept
    key = pc_number(pc)
    found = lotl.by_number.get(key or "")
    if found:
        return found[0], key, FILLED_NAME
    return None, None, NO_MATCH
