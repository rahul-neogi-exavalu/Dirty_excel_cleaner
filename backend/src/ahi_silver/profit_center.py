"""Profit Center Name and Number, populated from the LOTL as the AHI document says.

| Name    | Number  | Result                                                   |
|---------|---------|----------------------------------------------------------|
| present | present | kept; when the LOTL gives another number for the name, the LOTL's wins (corrected) |
| present | missing | number from the LOTL by name (filled_number)             |
| missing | missing | name (legacy_office_name) and number from the LOTL by pc_id (filled_name) |

Numbers are standardised to four digits (94 -> 0094). pc_id is the numeric part of the
source system, as a string (pc0515 -> 0515). A row the LOTL cannot settle is kept as is
and flagged, never dropped.
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


@dataclass
class Lotl:
    by_pc_id: dict[str, tuple[str, str]] = field(default_factory=dict)
    by_name: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows) -> "Lotl":
        lotl = cls()
        for pc_id, name, number in rows:
            key = pad(pc_id)
            number = pad(number)
            if key and name:
                lotl.by_pc_id[key] = (str(name).strip(), number)
            if name and number:
                lotl.by_name[str(name).strip().casefold()] = number
        return lotl

    @property
    def empty(self) -> bool:
        return not self.by_pc_id and not self.by_name


def pc_id(source_system: str | None) -> str | None:
    """Only the numeric portion of the source system, as a string."""
    digits = re.sub(r"\D", "", source_system or "")
    return digits or None


def pad(value) -> str | None:
    """Four-digit profit center number: 94 -> 0094, '94.0' -> 0094; other text kept."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+)(?:\.0+)?", text)
    return match.group(1).zfill(4) if match else text


def resolve(name, number, pc: str | None, lotl: Lotl) -> tuple[str | None, str | None, str]:
    """(name, number, status) for one row."""
    name = (str(name).strip() or None) if name is not None else None
    number = pad(number)
    if lotl.empty:
        return name, number, UNAVAILABLE
    if name and number:
        expected = lotl.by_name.get(name.casefold())
        if expected is None:
            return name, number, NO_MATCH
        return (name, number, KEPT) if expected == number else (name, expected, CORRECTED)
    if name:
        expected = lotl.by_name.get(name.casefold())
        return (name, expected, FILLED_NUMBER) if expected else (name, None, NO_MATCH)
    if number:
        return None, number, NAME_MISSING  # not a case the document covers: flagged, kept
    found = lotl.by_pc_id.get(pad(pc) or "")
    if found:
        return found[0], found[1], FILLED_NAME
    return None, None, NO_MATCH
