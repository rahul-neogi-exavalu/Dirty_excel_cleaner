"""Column names as words, so `acct_eff_date` and `Accounting Effective Date` can meet.

The cleaner's names run words together (`profitcentername`) and source systems
abbreviate (`acct`, `eff`, `pc no`). Names are split into words, concatenations are
separated with wordninja, and a small dictionary of the abbreviations these reports use
is expanded. Every matching step compares these word lists, never the raw names.
"""

from __future__ import annotations

import re
from functools import lru_cache

try:
    import wordninja
except ImportError:  # optional: without it, run-together names stay one word
    wordninja = None

# Abbreviations seen in AHI premium reports. Expanded whole-word only.
ABBREVIATIONS = {
    "acct": ["accounting"],
    "acctg": ["accounting"],
    "eff": ["effective"],
    "effdt": ["effective", "date"],
    "dt": ["date"],
    "pol": ["policy"],
    "trans": ["transaction"],
    "txn": ["transaction"],
    "pc": ["profit", "center"],
    "no": ["number"],
    "num": ["number"],
    "nbr": ["number"],
    "amt": ["amount"],
    "co": ["company"],
    "ins": ["insurance"],
    "prem": ["premium"],
    "comm": ["commission"],
    "lob": ["line", "of", "business"],
    "nm": ["name"],
    "cd": ["code"],
}


@lru_cache(maxsize=4096)
def words(name: str) -> tuple[str, ...]:
    tokens = [token for token in re.split(r"[^a-z0-9]+", (name or "").casefold()) if token]
    result: list[str] = []
    for token in tokens:
        if token in ABBREVIATIONS:
            result.extend(ABBREVIATIONS[token])
            continue
        parts = wordninja.split(token) if wordninja and len(token) > 3 and not token.isdigit() else [token]
        for part in parts:
            result.extend(ABBREVIATIONS.get(part, [part]))
    return tuple(result)


def phrase(name: str) -> str:
    """`acct_eff_date` -> `accounting effective date`."""
    return " ".join(words(name))


def compact(name: str) -> str:
    """`Accounting Effective Date` -> `accountingeffectivedate`, for exact comparison."""
    return "".join(words(name))
