"""How a new file's columns relate to a bronze table's: the four July cases.

1. IDENTICAL  -- same names, same order, same count: append as is.
2. REORDERED  -- same names and count, different order: reorder, then append.
3. EVOLVED    -- the table's names with columns added, or with some left out: add the
                 new columns to the table, fill the missing ones with NULL, append. When
                 the shared columns are also in another order (cases 2 and 3 at once),
                 ``reordered`` says so and they are put in the table's order too.
4. DIFFERENT  -- columns both added and missing (the names differ): the data goes to a
                 table of its own.

``ignore`` names columns the comparison leaves out on both sides: the cleaner's
``source_sheet`` provenance column, which a stacked multi-sheet file has and a one-sheet
file of the same report does not.
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
    # EVOLVED only: the shared columns are in another order as well.
    reordered: bool = False

    def as_dict(self) -> dict:
        return {"kind": self.kind, "added": list(self.added), "missing": list(self.missing),
                "reordered": self.reordered}


def compare(table: list[str], incoming: list[str], ignore=()) -> Comparison:
    skip = set(ignore)
    table = [name for name in table if name not in skip]
    incoming = [name for name in incoming if name not in skip]
    if list(table) == list(incoming):
        return Comparison(IDENTICAL)
    table_set, incoming_set = set(table), set(incoming)
    if table_set == incoming_set and len(table) == len(incoming):
        return Comparison(REORDERED)
    added = [name for name in incoming if name not in table_set]
    missing = [name for name in table if name not in incoming_set]
    # Case 3: only the count differs -- columns added, or left out, never both. Added
    # *and* missing means the names differ: a new schema.
    if (not added or not missing) and (table_set & incoming_set):
        return Comparison(EVOLVED, added, missing, reordered=not _in_order(table, incoming))
    return Comparison(DIFFERENT, added, missing)


def _in_order(table: list[str], incoming: list[str]) -> bool:
    incoming_set = set(incoming)
    shared = [name for name in table if name in incoming_set]
    return bool(shared) and shared == [name for name in incoming if name in set(table)]


def evolved_columns(table: list[str], incoming: list[str]) -> list[str]:
    """The table's columns after case 3: existing ones first, new ones appended."""
    return list(table) + [name for name in incoming if name not in set(table)]
