"""Decide where each cleaned table goes in the bronze layer, and how.

The rules are the AHI File -> Bronze scenario document's:

* A new table name -> CREATE.
* A file whose period overlaps one already loaded into that table (a revised Jan-Jun
  file, or Jan-Jul after Jan-Jun) -> REPLACE: the earlier file's rows are deleted and
  the new file loaded. Always confirmed by a person first.
* A later period (the July file) -> by schema: APPEND (identical), REORDER (same
  columns, other order), EVOLVE (columns added or missing; confirmed), or NEW_TABLE
  (different schema; confirmed).
* The exact same file again (same content hash) -> SKIP, unless the reviewer chooses
  to re-ingest it.

Files are planned oldest period first against a running picture of the bronze layer,
so several files in one batch with the same schema land in one table, and files with
different schemas get one table each. The planner is pure: the same inputs and the
same reviewer overrides always give the same plan.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class TableState:
    name: str
    columns: list[str]


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
    # Earlier ingestions this item deletes and supersedes.
    replaces: list[dict] = field(default_factory=list)
    # REPLACE removing every row of the table: drop and recreate it with the new columns.
    rebuild: bool = False
    requires_confirmation: bool = False
    reasons: list[str] = field(default_factory=list)
    # Anything that must be fixed before the plan can be approved.
    blockers: list[str] = field(default_factory=list)

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
        }


@dataclass
class _Table:
    columns: list[str]
    in_database: bool
    # (ingestion id or plan key, file name, period, from this plan?)
    loads: list[tuple[str, str, Period | None, bool]]


def plan(candidates: list[Candidate], tables: list[TableState], history: list[Ingested]) -> list[PlanItem]:
    state: dict[str, _Table] = {table.name: _Table(list(table.columns), True, []) for table in tables}
    for past in history:
        state.setdefault(past.table_name, _Table([], True, [])).loads.append(
            (past.id, past.file_name, past.period, False)
        )
    by_hash: dict[str, list[Ingested]] = {}
    for past in history:
        by_hash.setdefault(past.file_sha256, []).append(past)

    # Oldest period first; within one file the largest table first, so when two tables
    # of one sheet compete for a name, the main table keeps it and the side table
    # (a lookup beside it) gets the suffixed one.
    ordered = sorted(candidates, key=lambda c: (c.period.start if c.period else "9999", c.file_name, -c.rows, c.key))
    # The same file uploaded twice in one batch: (content hash, sheets) -> first file name.
    seen: dict[tuple[str, tuple[str, ...]], str] = {}
    return [_plan_one(candidate, state, by_hash, seen) for candidate in ordered]


def _plan_one(c: Candidate, state: dict[str, _Table], by_hash: dict[str, list[Ingested]],
              seen: dict[tuple[str, tuple[str, ...]], str]) -> PlanItem:
    source = naming.clean_source_system(c.source_system) or None
    suggested = naming.table_name(source or "", c.sheet_names)
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
    # only the one made from the same sheets is this output's duplicate.
    same_file = by_hash.get(c.file_sha256, []) if c.file_sha256 else []
    duplicates = [past for past in same_file if _same_output(past, c, target)]
    elsewhere = [past for past in same_file if past not in duplicates]
    table = state.get(target)
    twin = seen.get((c.file_sha256, tuple(c.sheet_names))) if c.file_sha256 else None

    if twin is not None:
        item.action, item.allowed_actions = SKIP, [SKIP]
        item.reasons.append(f"Same file as {twin}, already in this plan.")
        item.blockers = []
        return item
    if c.file_sha256:
        seen[(c.file_sha256, tuple(c.sheet_names))] = c.file_name
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
    _record(item, c, state)
    return item


def _same_output(past: Ingested, c: Candidate, target: str) -> bool:
    if past.source_sheets:
        return tuple(past.source_sheets) == tuple(c.sheet_names)
    return past.table_name == target  # older audit rows without sheets


def _duplicate(item: PlanItem, c: Candidate, duplicates: list[Ingested], state: dict[str, _Table]) -> None:
    first = duplicates[0]
    item.table_name = first.table_name
    item.action, item.allowed_actions = SKIP, [SKIP, REPLACE]
    item.reasons.append(f"This exact file was already ingested into {first.table_name} ({first.file_name}).")
    item.replaces = [_ref(past) for past in duplicates if past.table_name == first.table_name]
    table = state.get(first.table_name)
    if table:
        item.columns_before = list(table.columns)


def _existing(item: PlanItem, c: Candidate, table: _Table, state: dict[str, _Table]) -> None:
    item.columns_before = list(table.columns)
    comparison = compare(table.columns, c.columns)
    item.comparison = comparison.as_dict()
    overlapping = [load for load in table.loads if c.period and load[2] and load[2].overlaps(c.period)]
    from_db = [load for load in overlapping if not load[3]]
    from_plan = [load for load in overlapping if load[3]]

    if from_db:
        remaining = [load for load in table.loads if load not in from_db]
        item.replaces = [{"id": load[0], "file_name": load[1], "period_start": load[2].start,
                          "period_end": load[2].end} for load in from_db]
        item.requires_confirmation = True
        names = ", ".join(f"{load[1]} ({load[2].label()})" for load in from_db)
        item.reasons.append(f"Period overlaps what is already loaded: replaces {names}.")
        if not remaining:
            # Every row goes: the revised file defines the table again, columns and all.
            item.action, item.rebuild = REPLACE, True
            item.columns_after = list(c.columns)
            keep = [] if comparison.kind == DIFFERENT else [_kind_action(comparison.kind)]
            item.allowed_actions = [REPLACE, *keep, NEW_TABLE, SKIP]
            if comparison.kind != IDENTICAL:
                item.reasons.append("The table is recreated with the revised file's columns.")
            return
        if comparison.kind == DIFFERENT:
            _new_table(item, c, state, "Columns differ from the rest of the table.")
            item.allowed_actions = [NEW_TABLE, SKIP]
            return
        item.action = REPLACE
        item.columns_after = evolved_columns(table.columns, c.columns)
        item.allowed_actions = [REPLACE, APPEND if comparison.kind == IDENTICAL else _kind_action(comparison.kind), NEW_TABLE, SKIP]
        return

    if from_plan:
        item.requires_confirmation = True
        names = ", ".join(load[1] for load in from_plan)
        item.reasons.append(f"Same period as {names} in this batch; both will be kept.")

    if comparison.kind == DIFFERENT:
        _new_table(item, c, state, "Columns, order and count differ from the existing table.")
        item.requires_confirmation = item.requires_confirmation or table.in_database
        item.allowed_actions = [NEW_TABLE, SKIP]
        return
    item.action = _kind_action(comparison.kind)
    item.columns_after = evolved_columns(table.columns, c.columns)
    item.allowed_actions = [item.action, NEW_TABLE, SKIP]
    if comparison.kind == REORDERED:
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
    base = item.table_name
    suffix = c.period.start.replace("-", "_") if c.period else "v2"
    item.table_name = naming.next_free(naming.identifier(f"{base}_{suffix}"), set(state))
    item.action = NEW_TABLE
    item.columns_after = list(c.columns)
    item.columns_before = None
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
        item.columns_after = evolved_columns(item.columns_before or [], c.columns)
    else:
        item.action = wanted


def _record(item: PlanItem, c: Candidate, state: dict[str, _Table]) -> None:
    """Update the running picture of the bronze layer with this item's effect."""
    if item.action == SKIP or item.blockers:
        return
    table = state.get(item.table_name)
    replaced = {ref["id"] for ref in item.replaces}
    if table is None:
        state[item.table_name] = table = _Table([], False, [])
    if item.rebuild:
        table.loads = []
    table.loads = [load for load in table.loads if load[0] not in replaced]
    table.columns = list(item.columns_after)
    table.loads.append((c.key, c.file_name, c.period, True))


def _ref(past: Ingested) -> dict:
    return {
        "id": past.id,
        "file_name": past.file_name,
        "period_start": past.period.start if past.period else None,
        "period_end": past.period.end if past.period else None,
    }


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
