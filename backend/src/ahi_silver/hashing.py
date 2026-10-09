"""Business Key Hash and Row Hash.

Stored with every Silver row. As the AHI document notes, they are not used for
duplicate removal or record validation (yet) -- they are there so they can be.

Values are normalised before hashing (trimmed, case-folded, dates ISO, decimals with two
places -- enough for the decimal(10,6) rates --, NULL as an empty field, booleans as
true/false), so the same record always hashes the same.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal

SEPARATOR = "\x1f"


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return f"{value:.6f}"
    return str(value).strip().casefold()


def digest(values) -> str:
    return hashlib.sha256(SEPARATOR.join(_text(value) for value in values).encode("utf-8")).hexdigest()
