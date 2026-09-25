"""Per-column flags: every edge case a person reading the output should know about.

Two kinds, and the prefix says which:

* ``CHECK`` -- the cleaner made a judgement call the values could not settle (code or
  amount? month-first or day-first?) or had to leave values empty. Someone should look
  at the column, usually its header, and confirm.
* ``INFO`` -- the cleaner changed something on purpose and was sure of it (time of day
  dropped, currency symbols removed, dates normalised). Nothing to decide; worth knowing.

Flags are recorded in each table's trace under ``"flags"`` as ``{column: [flag, ...]}``
and surface in the metadata file's ``flags`` column, joined with `` | ``.
"""

from __future__ import annotations

CHECK = "CHECK"
INFO = "INFO"
SEPARATOR = " | "

# Kept as constants so adoption can find and replace them after a sibling's names arrive.
NO_HEADER = "no header row was found; column named by position"
BLANK_HEADER = "the header cell was blank; column named by position"


def check(text: str) -> str:
    return f"{CHECK}: {text}"


def info(text: str) -> str:
    return f"{INFO}: {text}"


def add(trace: dict, column: str, flag: str) -> None:
    """Attach one flag to a column, once."""
    column_flags = trace.setdefault("flags", {}).setdefault(column, [])
    if flag not in column_flags:
        column_flags.append(flag)


def add_to_all(trace: dict, columns, flag: str) -> None:
    for column in columns:
        add(trace, column, flag)


def example(values, limit: int = 3) -> str:
    """A short, quoted sample of offending values for a flag's text."""
    shown = [f"'{str(value)[:30]}'" for value in list(dict.fromkeys(values))[:limit]]
    return ", ".join(shown)
