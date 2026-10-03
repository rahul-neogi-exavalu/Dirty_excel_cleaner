"""Decide where each cleaned table goes in the bronze layer, and how.

The rules are the AHI File -> Bronze scenario document's:

* A new table name -> CREATE.
* A file whose period covers one already loaded into that table (a revised Jan-Jun
  file, or Jan-Jul after Jan-Jun) -> REPLACE: the earlier file's rows are deleted and
  the new file loaded. Always confirmed by a person first. A period that overlaps an
  earlier one only partly (May-Jul after Jan-Jun) is not a case the document covers:
  Replace would delete months the new file does not bring back and Append would load
  the shared months twice, so the reviewer must choose.
* A later period (the July file) -> by schema: APPEND (identical), REORDER (same
  columns, other order), EVOLVE (columns added or missing, in any order; confirmed), or
  NEW_TABLE (different schema; confirmed). A different schema first looks for another
  table of the same source it does fit, so a second report gets one table, not one per
  month.
* The exact same file again (same content hash, same table region) -> SKIP, unless the
  reviewer chooses to re-ingest it.

Files are planned oldest period first (Jan-Jun before Jan-Jul) against a running
picture of the bronze layer, so several files in one batch with the same schema land in
one table, and files with different schemas get one table each. A file whose period a
later file in the same batch covers is skipped by default. The planner is pure: the same
inputs and the same reviewer overrides always give the same plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import naming
from .periods import Period
from .schema_compare import DIFFERENT, EVOLVED, IDENTICAL, REORDERED, compare, evolved_columns

CREATE = "create"
APPEND = "append"
REORDER = "reorder"
EVOLVE = "evolve"
REPLACE = "replace"
NEW_TABLE = "new_table"
SKIP = "skip"

ACTIONS = (CREATE, APPEND, REORDER, EVOLVE, REPLACE, NEW_TABLE, SKIP)


@dataclass(frozen=True)
class Candidate:
    """One cleaned output offered for ingestion, with the reviewer's overrides."""

    key: str
    file_name: str
    file_sha256: str
    source_system: str | None
    sheet_names: list[str]
    # Bronze column identifiers, in the file's order.
    columns: list[str]
    rows: int
    period: Period | None
    table_override: str | None = None
    action_override: str | None = None
    # The table regions this output came from (two tables on one sheet are two regions).
    regions: tuple[str, ...] = ()
    # Bronze column -> its header as the file wrote it.
    headers: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    # The cleaner's sheet-provenance column, if this output has one.
    provenance: str | None = None


@dataclass(frozen=True)
class TableState:
    name: str
    columns: list[str]
    headers: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ingested:
    """A file already loaded into a bronze table and not superseded."""

    id: str
    table_name: str
    file_name: str
    file_sha256: str
    period: Period | None
    # The sheets the loaded table came from; with the hash, identifies one output of a file.
    source_sheets: tuple[str, ...] = ()
    # The table regions it came from (newer loads); tells two tables of one sheet apart.
    source_regions: tuple[str, ...] = ()


@dataclass
class PlanItem:
    key: str
    file_name: str
    source_system: str | None
    sheet_names: list[str]
    rows: int
    period: Period | None
    suggested_table: str
    table_name: str
    action: str
    allowed_actions: list[str]
    # How the file's columns compare with the table's, when the table already exists.
    comparison: dict | None = None
    # The table's columns once this item is loaded.
    columns_after: list[str] = field(default_factory=list)
    # The table's columns before, as the plan assumed them (checked again at load time).
    columns_before: list[str] | None = None
    # Earlier ingestions this item deletes and supersedes (each names its own table).
    replaces: list[dict] = field(default_factory=list)
    # REPLACE removing every row of the table: drop and recreate it with the new columns.
    rebuild: bool = False
    requires_confirmation: bool = False
    reasons: list[str] = field(default_factory=list)
    # Anything that must be fixed before the plan can be approved.
    blockers: list[str] = field(default_factory=list)
    # The file's column -> the table column it loads into, where the names differ only
    # because duplicate headers were numbered in another order (amount_2 is the table's amount).
    column_map: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "file_name": self.file_name,
            "source_system": self.source_system,
            "sheet_names": self.sheet_names,
            "rows": self.rows,
            "period_start": self.period.start if self.period else None,
            "period_end": self.period.end if self.period else None,
            "suggested_table": self.suggested_table,
            "table_name": self.table_name,
            "action": self.action,
            "allowed_actions": self.allowed_actions,
            "comparison": self.comparison,
            "columns_after": self.columns_after,
            "columns_before": self.columns_before,
            "replaces": self.replaces,
            "rebuild": self.rebuild,
            "requires_confirmation": self.requires_confirmation,
            "reasons": self.reasons,
            "blockers": self.blockers,
            "column_map": self.column_map,
        }


@dataclass
class _Table:
    columns: list[str]
    in_database: bool
    # (ingestion id or plan key, file name, period, from this plan?)
    loads: list[tuple[str, str, Period | None, bool]]
    headers: dict[str, str] = field(default_factory=dict)
    provenance: set[str] = field(default_factory=set)


def plan(candidates: list[Candidate], tables: list[TableState], history: list[Ingested]) -> list[PlanItem]:
    state: dict[str, _Table] = {
        table.name: _Table(list(table.columns), True, [], dict(table.headers), set(table.provenance)) for table in tables
    }
    for past in history:
        state.setdefault(past.table_name, _Table([], True, [])).loads.append(
            (past.id, past.file_name, past.period, False)
        )
    by_hash: dict[str, list[Ingested]] = {}
    for past in history:
        by_hash.setdefault(past.file_sha256, []).append(past)

    # Oldest period first, the shorter of two with the same start first (Jan-Jun before
    # Jan-Jul, so the later file is the one that replaces); within one file the largest
    # table first, so when two tables of one sheet compete for a name, the main table
    # keeps it and the side table (a lookup beside it) gets the suffixed one.
    ordered = sorted(candidates, key=lambda c: (c.period.start if c.period else "9999",
                                                c.period.end if c.period else "9999", c.file_name, -c.rows, c.key))
    covered = _covered_in_batch(ordered, state)
    # The same file uploaded twice in one batch: (content hash, sheets, regions) -> first file name.
    seen: dict[tuple, str] = {}
    return [_plan_one(candidate, state, by_hash, seen, covered.get(candidate.key)) for candidate in ordered]


def _years(c: Candidate) -> set[int] | None:
    if not c.period:
        return None
    return set(range(int(c.period.start[:4]), int(c.period.end[:4]) + 1))


def _suggested(c: Candidate, state: dict[str, _Table]) -> str:
    """The convention's table name; a long-named table made before identifiers were
    hashed with SHA-256 keeps its older (SHA-1) name."""
    source = naming.clean_source_system(c.source_system) or ""
    name = naming.table_name(source, c.sheet_names, years=_years(c))
    if name in state:
        return name
    legacy = naming.table_name(source, c.sheet_names, years=_years(c), legacy=True)
    return legacy if legacy in state else name


def _covered_in_batch(ordered: list[Candidate], state: dict[str, _Table]) -> dict[str, str]:
    """Files whose period a later, different file of the batch covers, for the same
    table: key -> that file's name. They are skipped unless the reviewer says otherwise."""
    targets = {c.key: (naming.identifier(c.table_override) if c.table_override else _suggested(c, state))
               for c in ordered}
    covered = {}
    for index, c in enumerate(ordered):
        if not c.period:
            continue
        for later in ordered[index + 1:]:
            if (later.period and later.file_sha256 != c.file_sha256 and targets[later.key] == targets[c.key]
                    and later.period.covers(c.period) and later.period != c.period):
                covered[c.key] = later.file_name
                break
    return covered


def _plan_one(c: Candidate, state: dict[str, _Table], by_hash: dict[str, list[Ingested]],
              seen: dict[tuple, str], covered_by: str | None = None) -> PlanItem:
    source = naming.clean_source_system(c.source_system) or None
    suggested = _suggested(c, state)
    target = naming.identifier(c.table_override) if c.table_override else suggested
    item = PlanItem(
        key=c.key, file_name=c.file_name, source_system=source, sheet_names=list(c.sheet_names),
        rows=c.rows, period=c.period, suggested_table=suggested, table_name=target,
        action=CREATE, allowed_actions=[CREATE, SKIP], columns_after=list(c.columns),
    )
    if not source:
        item.blockers.append("Enter the source system.")
    if c.period is None:
        item.blockers.append("Confirm the period this file covers.")

    # A file with several tables has one ingestion per table, all with the same hash:
    # only the one made from the same table region (or, for older loads, the same
    # sheets) is this output's duplicate.
    same_file = by_hash.get(c.file_sha256, []) if c.file_sha256 else []
    duplicates = [past for past in same_file if _same_output(past, c, target)]
    elsewhere = [past for past in same_file if past not in duplicates]
    table = state.get(target)
    twin_key = (c.file_sha256, tuple(c.sheet_names), tuple(c.regions))
    twin = seen.get(twin_key) if c.file_sha256 else None

    if twin is not None:
        item.action, item.allowed_actions = SKIP, [SKIP]
        item.reasons.append(f"Same file as {twin}, already in this plan.")
        item.blockers = []
        return item
    if c.file_sha256:
        seen[twin_key] = c.file_name
    if elsewhere and not duplicates:
        item.reasons.append(f"This file was already ingested into {elsewhere[0].table_name}.")

    if duplicates:
        _duplicate(item, c, duplicates, state)
    elif table is None or (not table.columns and not table.loads):
        item.action, item.allowed_actions = CREATE, [CREATE, SKIP]
        if c.table_override and c.table_override != suggested:
            item.reasons.append(f"New table named by the reviewer instead of {suggested}.")
    else:
        _existing(item, c, table, state)

    _apply_override(item, c, state)
    if covered_by and not c.action_override and item.action != SKIP:
        item.allowed_actions = [SKIP] + [action for action in item.allowed_actions if action != SKIP]
        item.action, item.replaces, item.rebuild = SKIP, [], False
        item.blockers = []
        item.reasons.append(f"{covered_by} in this batch covers this file's period and replaces it; "
                            "choose another action to load this file as well.")
    _record(item, c, state)
    return item


def _same_output(past: Ingested, c: Candidate, target: str) -> bool:
    if past.source_regions and c.regions:
        return tuple(past.source_regions) == tuple(c.regions)
    if past.source_sheets:
        return tuple(past.source_sheets) == tuple(c.sheet_names)
    return past.table_name == target  # older audit rows without sheets


def _ref(past_id: str, file_name: str, period: Period | None, table_name: str) -> dict:
    return {
        "id": past_id,
        "file_name": file_name,
        "period_start": period.start if period else None,
        "period_end": period.end if period else None,
        "table_name": table_name,
    }


def _duplicate(item: PlanItem, c: Candidate, duplicates: list[Ingested], state: dict[str, _Table]) -> None:
    first = duplicates[0]
    item.table_name = first.table_name
    item.action, item.allowed_actions = SKIP, [SKIP, REPLACE]
    item.reasons.append(f"This exact file was already ingested into {first.table_name} ({first.file_name}).")
    item.replaces = [_ref(past.id, past.file_name, past.period, past.table_name)
                     for past in duplicates if past.table_name == first.table_name]
    table = state.get(first.table_name)
    if table:
        item.columns_before = list(table.columns)


def _header_key(text: str | None) -> str:
    return " ".join(str(text or "").split()).casefold()


_NUMBERED = re.compile(r"^(.*?)(?:_\d+)?$")


def _column_map(c: Candidate, table: _Table) -> dict[str, str]:
    """Rename the file's columns to the table's where only the numbering of duplicate
    headers differs, or where the table kept an older (SHA-1) shortened identifier.

    Duplicate headers are numbered in the order they appear ('Amount ($)' -> amount,
    'Amount (%)' -> amount_2). If the next file lists them the other way round, the
    names alone would say IDENTICAL and append each column into the other. The headers
    as written tell them apart: a file column moves to the table column with its header
    and the same base name.
    """
    mapping: dict[str, str] = {}
    by_header: dict[tuple[str, str], str] = {}
    for name in table.columns:
        header = table.headers.get(name)
        if header:
            by_header.setdefault((_NUMBERED.match(name).group(1), _header_key(header)), name)
    for name in c.columns:
        header = c.headers.get(name)
        if not header:
            continue
        found = by_header.get((_NUMBERED.match(name).group(1), _header_key(header)))
        if found and found != name:
            mapping[name] = found
    # Only a reshuffle among the file's own columns: never two onto one, never onto a
    # column the file also keeps under its own name.
    targets = list(mapping.values())
    kept = {name for name in c.columns if name not in mapping}
    if len(set(targets)) != len(targets) or kept & set(targets):
        mapping = {}
    # A long column name the table stored under its older shortened identifier.
    known = set(table.columns)
    for name in c.columns:
        if name in mapping or name in known or len(name) < naming.MAX_IDENTIFIER:
            continue
        for column in table.columns:
            if len(column) == naming.MAX_IDENTIFIER and column[:-9] == name[:-9] and column not in c.columns:
                mapping[name] = column
                break
    return mapping


def _existing(item: PlanItem, c: Candidate, table: _Table, state: dict[str, _Table]) -> None:
    item.columns_before = list(table.columns)
    item.column_map = _column_map(c, table)
    if item.column_map:
        item.reasons.append("Columns with repeated headers are matched by their headers: "
                            + ", ".join(f"{a} -> {b}" for a, b in item.column_map.items()) + ".")
    incoming = [item.column_map.get(name, name) for name in c.columns]
    provenance = table.provenance | ({c.provenance} if c.provenance else set())
    comparison = compare(table.columns, incoming, ignore=provenance)
    item.comparison = comparison.as_dict()
    overlapping = [load for load in table.loads if c.period and load[2] and load[2].overlaps(c.period)]
    from_db = [load for load in overlapping if not load[3]]
    from_plan = [load for load in overlapping if load[3]]
    if c.provenance and c.provenance not in table.columns:
        item.reasons.append(f"Adds the cleaner's sheet column {c.provenance} to the table.")

    if from_db:
        remaining = [load for load in table.loads if load not in from_db]
        item.replaces = [_ref(load[0], load[1], load[2], item.table_name) for load in from_db]
        item.requires_confirmation = True
        names = ", ".join(f"{load[1]} ({load[2].label()})" for load in from_db)
        item.reasons.append(f"Period overlaps what is already loaded: replaces {names}.")
        partial = [load for load in from_db if not c.period.covers(load[2])]
        if partial and not c.action_override:
            for load in partial:
                item.blockers.append(
                    f"{c.period.label()} overlaps {load[1]} ({load[2].label()}) only partly: Replace deletes all of "
                    f"{load[1]}, including months this file does not have; Append loads the shared months twice. "
                    "Choose the action.")
        if not remaining:
            # Every row goes: the revised file defines the table again, columns and all.
            item.action, item.rebuild = REPLACE, True
            item.columns_after = list(incoming)
            keep = [] if comparison.kind == DIFFERENT else [_kind_action(comparison.kind)]
            item.allowed_actions = [REPLACE, *keep, NEW_TABLE, SKIP]
            if comparison.kind != IDENTICAL:
                item.reasons.append("The table is recreated with the revised file's columns.")
            return
        if comparison.kind == DIFFERENT:
            # The earlier file's rows still go, from the table they are in.
            _new_table(item, c, state, "Columns differ from the rest of the table.")
            if item.action == NEW_TABLE:
                item.allowed_actions = [NEW_TABLE, SKIP]
            return
        item.action = REPLACE
        item.columns_after = evolved_columns(table.columns, incoming)
        item.allowed_actions = [REPLACE, APPEND if comparison.kind == IDENTICAL else _kind_action(comparison.kind), NEW_TABLE, SKIP]
        return

    if from_plan:
        item.requires_confirmation = True
        names = ", ".join(load[1] for load in from_plan)
        item.reasons.append(f"Same period as {names} in this batch; both will be kept.")

    if comparison.kind == DIFFERENT:
        _new_table(item, c, state, "Columns, order and count differ from the existing table.")
        item.requires_confirmation = item.requires_confirmation or table.in_database
        if item.action == NEW_TABLE:
            item.allowed_actions = [NEW_TABLE, SKIP]
        return
    item.action = _kind_action(comparison.kind)
    item.columns_after = evolved_columns(table.columns, incoming)
    item.allowed_actions = [item.action, NEW_TABLE, SKIP]
    if comparison.kind == REORDERED or comparison.reordered:
        item.reasons.append("Columns are reordered to the table's order before appending.")
    if comparison.kind == EVOLVED:
        if comparison.added:
            item.reasons.append(f"Adds {len(comparison.added)} column(s) to the table: {', '.join(comparison.added)}.")
        if comparison.missing:
            item.reasons.append(f"{len(comparison.missing)} column(s) not in this file are left empty: {', '.join(comparison.missing)}.")
        item.requires_confirmation = item.requires_confirmation or table.in_database


def _kind_action(kind: str) -> str:
    return {IDENTICAL: APPEND, REORDERED: REORDER, EVOLVED: EVOLVE}.get(kind, NEW_TABLE)


def _new_table(item: PlanItem, c: Candidate, state: dict[str, _Table], why: str) -> None:
    """A table of its own for a file that does not fit its table: first another table of
    the same source it does fit (a second report's own table), else ``<base>_v2``."""
    base = item.table_name
    item.rebuild = False
    source = naming.clean_source_system(c.source_system)
    prefix = f"{naming.PREFIX}_{source}_" if source else None
    for name, table in sorted(state.items()):
        if name == base or not prefix or not name.startswith(prefix) or not table.columns:
            continue
        mapping = _column_map(c, table)
        provenance = table.provenance | ({c.provenance} if c.provenance else set())
        fits = compare(table.columns, [mapping.get(n, n) for n in c.columns], ignore=provenance)
        if fits.kind != DIFFERENT:
            replaces = list(item.replaces)
            item.table_name = name
            item.reasons.append(f"{why} It fits {name}, which already holds this source's other report.")
            _existing(item, c, table, state)
            item.replaces = replaces + [ref for ref in item.replaces if ref not in replaces]
            return
    taken = set(state)
    number = 2
    while (name := naming.identifier(f"{base}_v{number}")) in taken:
        number += 1
    item.table_name = name
    item.action = NEW_TABLE
    item.columns_after = list(c.columns)
    item.columns_before = None
    item.column_map = {}
    item.reasons.append(f"{why} Loaded into a separate table, {item.table_name}.")


def _apply_override(item: PlanItem, c: Candidate, state: dict[str, _Table]) -> None:
    wanted = c.action_override
    if not wanted or wanted == item.action:
        return
    if wanted not in item.allowed_actions:
        item.blockers.append(f"'{wanted}' is not possible for this file; choose one of {', '.join(item.allowed_actions)}.")
        return
    item.requires_confirmation = True
    item.reasons.append(f"Action changed by the reviewer from {item.action} to {wanted}.")
    if wanted == SKIP:
        item.action = SKIP
    elif wanted == NEW_TABLE:
        _new_table(item, c, state, "Reviewer chose a separate table.")
    elif wanted == REPLACE and item.action == SKIP:
        # Re-ingest a file that was already loaded: its earlier load is replaced.
        table = state.get(item.table_name)
        remaining = [load for load in (table.loads if table else []) if load[0] not in {r["id"] for r in item.replaces}]
        item.action, item.rebuild = REPLACE, not remaining
        item.columns_after = list(c.columns) if not remaining else evolved_columns(item.columns_before or [], c.columns)
    elif wanted in (APPEND, REORDER, EVOLVE):
        # Keep the overlapping earlier load instead of replacing it.
        item.action = wanted
        item.replaces, item.rebuild = [], False
        incoming = [item.column_map.get(name, name) for name in c.columns]
        item.columns_after = evolved_columns(item.columns_before or [], incoming)
    else:
        item.action = wanted


def _record(item: PlanItem, c: Candidate, state: dict[str, _Table]) -> None:
    """Update the running picture of the bronze layer with this item's effect."""
    if item.action == SKIP or item.blockers:
        return
    replaced = {ref["id"] for ref in item.replaces}
    # A replaced load leaves whichever table it is in (a separate table's file replaces
    # one in the table it did not fit).
    for other in state.values():
        other.loads = [load for load in other.loads if load[0] not in replaced]
    table = state.get(item.table_name)
    if table is None:
        state[item.table_name] = table = _Table([], False, [])
    if item.rebuild:
        table.loads = []
    table.columns = list(item.columns_after)
    for name, header in c.headers.items():
        table.headers.setdefault(item.column_map.get(name, name), header)
    if c.provenance:
        table.provenance.add(c.provenance)
    table.loads.append((c.key, c.file_name, c.period, True))


def approvable(items: list[PlanItem], confirmed: set[str]) -> list[str]:
    """Why the plan cannot be approved yet; empty when it can."""
    problems = []
    for item in items:
        if item.action == SKIP:
            continue  # nothing is written for it, so nothing to fix or confirm
        for blocker in item.blockers:
            problems.append(f"{item.file_name}: {blocker}")
        if item.requires_confirmation and item.key not in confirmed:
            problems.append(f"{item.file_name}: confirm the {item.action.replace('_', ' ')} into {item.table_name}.")
    return problems
