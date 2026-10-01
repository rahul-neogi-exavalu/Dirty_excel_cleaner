"""Business Key Hash and Row Hash.

Stored with every Silver row. As the AHI document notes, they are not used for
duplicate removal or record validation (yet) -- they are there so they can be.

Values are normalised before hashing (trimmed, case-folded, dates ISO, decimals with two
places, NULL as an empty field), so the same record always hashes the same.
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
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return str(value).strip().casefold()


def digest(values) -> str:
    return hashlib.sha256(SEPARATOR.join(_text(value) for value in values).encode("utf-8")).hexdigest()
