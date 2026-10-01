"""How a new file's columns relate to a bronze table's: the four July cases.

1. IDENTICAL  -- same names, same order, same count: append as is.
2. REORDERED  -- same names and count, different order: reorder, then append.
3. EVOLVED    -- same names in the same order, but columns added or missing: add the
                 new columns to the table, fill the missing ones with NULL, append.
4. DIFFERENT  -- anything else: the data goes to a table of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

IDENTICAL = "identical"
REORDERED = "reordered"
EVOLVED = "evolved"
DIFFERENT = "different"


@dataclass(frozen=True)
class Comparison:
    kind: str
    added: list[str] = field(default_factory=list)  # in the file, not yet in the table
    missing: list[str] = field(default_factory=list)  # in the table, not in the file

    def as_dict(self) -> dict:
        return {"kind": self.kind, "added": list(self.added), "missing": list(self.missing)}


def compare(table: list[str], incoming: list[str]) -> Comparison:
    if list(table) == list(incoming):
        return Comparison(IDENTICAL)
    table_set, incoming_set = set(table), set(incoming)
    if table_set == incoming_set and len(table) == len(incoming):
        return Comparison(REORDERED)
    added = [name for name in incoming if name not in table_set]
    missing = [name for name in table if name not in incoming_set]
    # Case 3: only the count differs -- one column list is the other with columns added
    # (or left out), the rest in the same order. Added *and* missing is a new schema.
    if (not added or not missing) and _in_order(table, incoming):
        return Comparison(EVOLVED, added, missing)
    return Comparison(DIFFERENT, added, missing)


def _in_order(table: list[str], incoming: list[str]) -> bool:
    incoming_set = set(incoming)
    shared = [name for name in table if name in incoming_set]
    return bool(shared) and shared == [name for name in incoming if name in set(table)]


def evolved_columns(table: list[str], incoming: list[str]) -> list[str]:
    """The table's columns after case 3: existing ones first, new ones appended."""
    return list(table) + [name for name in incoming if name not in set(table)]
