"""How a new file's columns relate to a bronze table's: the four July cases.

1. IDENTICAL  -- same names, same order, same count: append as is.
2. REORDERED  -- same names and count, different order: reorder, then append.
3. EVOLVED    -- the file and the table share at least one column: add whichever of the
                 file's columns the table lacks, fill whichever of the table's columns
                 the file lacks with NULL, append. This holds regardless of how many
                 columns are added, missing, or both at once -- there is no cutoff on how
                 much may differ, only on whether anything at all is shared. When the
                 shared columns are also in another order, ``reordered`` says so and they
                 are put in the table's order too.
4. DIFFERENT  -- nothing is shared at all: an unrelated schema, so the data goes to a
                 table of its own.

``common_percent`` is how much of the two column sets overlap (the shared columns over
their union, as a percentage) -- shown to the reviewer whatever the kind, so they can
judge a borderline EVOLVED for themselves instead of the comparison picking a cutoff for
them.

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
    # The shared columns over their union, as a percentage: how much the two overlap.
    common_percent: float = 100.0

    def as_dict(self) -> dict:
        return {"kind": self.kind, "added": list(self.added), "missing": list(self.missing),
                "reordered": self.reordered, "common_percent": self.common_percent}


def compare(table: list[str], incoming: list[str], ignore=()) -> Comparison:
    skip = set(ignore)
    table = [name for name in table if name not in skip]
    incoming = [name for name in incoming if name not in skip]
    table_set, incoming_set = set(table), set(incoming)
    shared = table_set & incoming_set
    union = table_set | incoming_set
    common_percent = round(100 * len(shared) / len(union), 1) if union else 100.0
    if list(table) == list(incoming):
        return Comparison(IDENTICAL, common_percent=common_percent)
    if table_set == incoming_set and len(table) == len(incoming):
        return Comparison(REORDERED, common_percent=common_percent)
    added = [name for name in incoming if name not in table_set]
    missing = [name for name in table if name not in incoming_set]
    # Any column at all in common: the file still belongs in this table -- its new
    # columns are added, the ones it lacks stay empty. However much is added or missing,
    # that never on its own sends the file to a table of its own; only sharing *nothing*
    # does.
    if shared:
        return Comparison(EVOLVED, added, missing, reordered=not _in_order(table, incoming),
                          common_percent=common_percent)
    return Comparison(DIFFERENT, added, missing, common_percent=common_percent)


def _in_order(table: list[str], incoming: list[str]) -> bool:
    incoming_set = set(incoming)
    shared = [name for name in table if name in incoming_set]
    return bool(shared) and shared == [name for name in incoming if name in set(table)]


def evolved_columns(table: list[str], incoming: list[str]) -> list[str]:
    """The table's columns after case 3: existing ones first, new ones appended."""
    return list(table) + [name for name in incoming if name not in set(table)]
