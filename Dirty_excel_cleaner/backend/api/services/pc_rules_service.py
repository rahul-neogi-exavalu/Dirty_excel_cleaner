"""Per-profit-center Silver rules: the store, the seed files, and what a Silver review needs.

The rules themselves -- what each kind does, how they are checked and applied -- are in
``ahi_silver.rules``. Here: ``<control>.silver_pc_rule`` (one row per rule) and
``<control>.silver_pc_rule_version`` (bumped by every change, so a review approved
against older rules is refused rather than loaded with values nobody saw), the YAML seed
files in ``backend/config/pc_rules``, and the rows a ``bronze_table`` lookup reads.
"""

from __future__ import annotations

import fnmatch
import threading
from pathlib import Path

from ahi_silver import rule_sql
from ahi_silver import rules as pc_rules
from ahi_silver.rules import Rule, RuleError

from .. import config, db
from ..errors import ApiError, conflict, not_found

TABLE = "silver_pc_rule"
VERSION = "silver_pc_rule_version"
SEEDS = config.ROOT / "config" / "pc_rules"
# Table column -> Rule field (the table says column_name and rule_key, the Rule column and key).
_FIELDS = [("id", "id"), ("profit_center", "profit_center"), ("rule_key", "key"), ("kind", "kind"),
           ("target", "target"), ("column_name", "column"), ("scope_bronze_table", "scope_bronze_table"),
           ("scope_file_like", "scope_file_like"), ("scope_sheet", "scope_sheet"), ("seq", "seq"), ("spec", "spec"),
           ("enabled", "enabled"), ("note", "note"), ("source_ref", "source_ref"), ("approved_by", "approved_by"),
           ("approved_at", "approved_at"), ("sql_text", "sql"), ("has_sql", "has_sql"), ("created_at", "created_at"),
           ("modified_at", "modified_at"), ("is_deleted", "is_deleted"), ("deleted_at", "deleted_at"),
           ("deleted_by", "deleted_by")]
_SELECT = ", ".join(column for column, _ in _FIELDS)
# What the store keeps about a rule that is not the rule itself (never given to Rule).
_META = ("id", "approved_by", "approved_at", "sql", "has_sql", "created_at", "modified_at", "is_deleted",
         "deleted_at", "deleted_by")
_TIMES = ("approved_at", "created_at", "modified_at", "deleted_at")
_EDITABLE = [field for _, field in _FIELDS if field not in _META]
# A rule that is not deleted: the only kind applied, listed, seeded over or exported.
_LIVE = "NOT is_deleted"

_cache: dict = {"version": None, "rules": [], "broken": []}
_cache_lock = threading.Lock()
_lookup_cache: dict[tuple, list[dict]] = {}


def _ident(name: str):
    from psycopg import sql

    return sql.Identifier(name)


def _table(name: str = TABLE):
    from psycopg import sql

    return sql.SQL("{}.{}").format(_ident(config.CONTROL_SCHEMA), _ident(name))


def allowed() -> dict:
    """The Silver columns a rule may write, per target."""
    from . import silver_service

    return pc_rules.allowed_columns(silver_service.catalog(), silver_service.aggregate_catalog())


# --- reading -----------------------------------------------------------------------------


def version(conn) -> int | None:
    """The rules' version; None before the migration has run."""
    from psycopg import sql

    if conn.execute("SELECT to_regclass(%s)", [f"{config.CONTROL_SCHEMA}.{VERSION}"]).fetchone()[0] is None:
        return None
    row = conn.execute(sql.SQL("SELECT version FROM {}").format(_table(VERSION))).fetchone()
    return int(row[0]) if row else 0


def _bump(conn) -> int:
    from psycopg import sql

    row = conn.execute(sql.SQL("UPDATE {} SET version = version + 1, changed = now() RETURNING version")
                       .format(_table(VERSION))).fetchone()
    with _cache_lock:
        _cache["version"] = None
        _lookup_cache.clear()
    return int(row[0])


def _as_dict(row) -> dict:
    return {field: value for (_, field), value in zip(_FIELDS, row)}


def _rule(data: dict) -> Rule:
    return Rule.from_dict({k: v for k, v in data.items() if k not in _META})


def sql_of(rule: Rule, written: str | None = None) -> str | None:
    """The rule's SQL for the store: what the reviewer wrote (its spec was read from it),
    else the SQL its JSON reads as; None when it has no exact SQL form."""
    if written and written.strip():
        return written.strip()
    try:
        return rule_sql.to_sql(rule.kind, rule.spec)
    except rule_sql.NoSql:
        return None


def _public(data: dict) -> dict:
    return {**data, **{name: data[name].isoformat() if hasattr(data.get(name), "isoformat")
                       else (str(data[name]) if data.get(name) else None) for name in _TIMES}}


def all_rules(conn) -> tuple[int | None, list[Rule], list[str]]:
    """(version, enabled rules, problems): read once per version, then from memory. A
    stored rule that no longer validates (the Silver columns changed) is left out and
    named in the problems, never applied half-way."""
    from psycopg import sql

    current = version(conn)
    if current is None:
        return None, [], []
    with _cache_lock:
        if _cache["version"] == current:
            return current, list(_cache["rules"]), list(_cache["broken"])
    rows = conn.execute(sql.SQL("SELECT {} FROM {} WHERE enabled AND {} ORDER BY profit_center, seq, rule_key")
                        .format(sql.SQL(_SELECT), _table(), sql.SQL(_LIVE))).fetchall()
    limits = allowed()
    rules, broken = [], []
    for row in rows:
        data = _as_dict(row)
        try:
            rule = _rule(data)
            errors = pc_rules.validate(rule, limits)
        except (RuleError, TypeError) as error:
            errors = [str(error)]
            rule = None
        if errors:
            broken.append(f"{data['profit_center']} {data['key']}: {'; '.join(errors)}")
        else:
            rules.append(rule)
    with _cache_lock:
        _cache.update(version=current, rules=rules, broken=broken)
    return current, list(rules), list(broken)


def for_review(conn, pcs: list[str], batch_ids: list[str]) -> tuple[list[Rule], dict[str, list[dict]], list[str]]:
    """The rules of the review's profit centers, the rows their ``bronze_table`` lookups
    read, and notes for the reviewer."""
    _, rules, broken = all_rules(conn)
    wanted = {pc_rules.normalize_pc(pc) for pc in pcs if pc}
    chosen = [rule for rule in rules if pc_rules.normalize_pc(rule.profit_center) in wanted]
    notes = [f"Rule left out: {problem}" for problem in broken
             if any(problem.startswith(pc + " ") for pc in wanted)]
    lookups: dict[str, list[dict]] = {}
    for rule in chosen:
        if rule.kind == pc_rules.LOOKUP and rule.spec.get("source") == "bronze_table":
            rows, note = _lookup_rows(conn, rule, batch_ids)
            lookups[rule.key] = rows
            if note:
                notes.append(note)
    return chosen, lookups, notes


def _lookup_rows(conn, rule: Rule, batch_ids: list[str]) -> tuple[list[dict], str | None]:
    """The rows of the bronze table a lookup names: the load in this batch, else the
    table's latest ingested load. Keyed by the file's headers (else the column names)."""
    from psycopg import sql

    pattern = str(rule.spec["table"]).casefold()
    registry = conn.execute(sql.SQL("SELECT table_name, columns FROM {}.bronze_table")
                            .format(_ident(config.CONTROL_SCHEMA))).fetchall()
    tables = [(name, columns) for name, columns in registry if fnmatch.fnmatchcase(name.casefold(), pattern)]
    own = (rule.profit_center or "").casefold()
    tables = [t for t in tables if own in t[0].casefold()] or tables
    if not tables:
        return [], f"{rule.key}: no bronze table matches {rule.spec['table']!r}; the lookup fills nothing."
    names = [t[0] for t in tables]
    found = conn.execute(sql.SQL(
        "SELECT id, table_name FROM {}.ingestion WHERE table_name = ANY(%s) AND status = 'ingested' "
        "ORDER BY (id = ANY(%s)) DESC, coalesce(processing_date, created_at) DESC LIMIT 1")
        .format(_ident(config.CONTROL_SCHEMA)), [names, batch_ids]).fetchone()
    if not found:
        return [], f"{rule.key}: {', '.join(names)} has no ingested load; the lookup fills nothing."
    load_id, table = found
    key = (table, load_id)
    if key in _lookup_cache:
        return _lookup_cache[key], None
    columns = next(c for n, c in tables if n == table)
    column_names = [c["name"] for c in columns]
    labels = [c.get("source_header") or c["name"] for c in columns]
    rows = conn.execute(sql.SQL("SELECT {} FROM {}.{} WHERE _ingestion_id = %s").format(
        sql.SQL(", ").join(_ident(n) for n in column_names), _ident(config.BRONZE_SCHEMA), _ident(table)),
        [load_id]).fetchall()
    result = [dict(zip(labels, row)) for row in rows]
    _lookup_cache[key] = result
    return result, None


# --- the store ------------------------------------------------------------------------------


def list_rules(profit_center: str | None = None, include_deleted: bool = False) -> list[dict]:
    """The rules, by profit center; deleted ones (history) only with ``include_deleted``."""
    db.require()
    from psycopg import sql

    where, params = [] if include_deleted else [sql.SQL(_LIVE)], []
    if profit_center:
        pc = pc_rules.normalize_pc(profit_center)
        if not pc:
            raise ApiError(422, "bad_profit_center", f"{profit_center} is not a profit center.", "Use PC2041.")
        where.append(sql.SQL("profit_center = %s"))
        params.append(pc)
    clause = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL("")
    with db.connection() as conn:
        rows = conn.execute(sql.SQL("SELECT {} FROM {}{} ORDER BY profit_center, kind, seq, rule_key, deleted_at")
                            .format(sql.SQL(_SELECT), _table(), clause), params).fetchall()
        current = version(conn)
    return [{**_public(_as_dict(row)), "version": current} for row in rows]


def validate(data: dict) -> list[str]:
    """Everything wrong with a rule as submitted; empty when it can be saved."""
    try:
        rule = _rule(data)
    except (RuleError, TypeError) as error:
        return [str(error)]
    rule.profit_center = pc_rules.normalize_pc(rule.profit_center) or rule.profit_center
    return pc_rules.validate(rule, allowed())


def with_sql(data: dict) -> dict:
    """The rule as submitted, its spec read from ``sql`` when the reviewer wrote SQL: the
    JSON is always what runs, so SQL is turned into it before anything is checked."""
    written = (data.get("sql") or "").strip()
    if not written:
        return data
    try:
        spec = rule_sql.from_sql(data.get("kind"), written, data.get("spec"))
    except rule_sql.SqlError as error:
        raise ApiError(422, "invalid_rule_sql", "The rule's SQL cannot be read.", error.message) from None
    return {**data, "spec": spec, "sql": written}


def save_rule(data: dict, user: str, rule_id: int | None = None) -> dict:
    """Add a rule, or replace rule ``rule_id``; recorded as approved by ``user``. With
    ``sql`` the spec is read from it; either way the SQL stored is kept in step."""
    db.require()
    from psycopg import errors, sql
    from psycopg.types.json import Jsonb

    data = with_sql(data)
    errors_found = validate(data)
    if errors_found:
        raise ApiError(422, "invalid_rule", "The rule cannot be applied as written.", "; ".join(errors_found))
    rule = _rule(data)
    rule.profit_center = pc_rules.normalize_pc(rule.profit_center)
    values = _values(rule)
    text = sql_of(rule, data.get("sql"))
    with db.connection() as conn:
        try:
            if rule_id is None:
                row = conn.execute(sql.SQL(
                    "INSERT INTO {} ({}, sql_text, has_sql, approved_by, approved_at, created_at, modified_at) "
                    "VALUES ({}, %s, %s, %s, now(), now(), now()) RETURNING {}").format(
                    _table(), sql.SQL(", ").join(_ident(c) for c in values),
                    sql.SQL(", ").join(sql.Placeholder() for _ in values), sql.SQL(_SELECT)),
                    [Jsonb(v) if c == "spec" else v for c, v in values.items()] + [text, text is not None, user]).fetchone()
            else:
                row = conn.execute(sql.SQL(
                    "UPDATE {} SET {}, sql_text = %s, has_sql = %s, approved_by = %s, approved_at = now(), "
                    "modified_at = now() WHERE id = %s AND {} RETURNING {}").format(
                    _table(), sql.SQL(", ").join(sql.SQL("{} = %s").format(_ident(c)) for c in values),
                    sql.SQL(_LIVE), sql.SQL(_SELECT)),
                    [Jsonb(v) if c == "spec" else v for c, v in values.items()]
                    + [text, text is not None, user, rule_id]).fetchone()
                if row is None:
                    raise not_found(f"Rule {rule_id}")
        except errors.UniqueViolation:
            raise conflict(f"{rule.profit_center} already has a rule {rule.key!r}.", "Choose another key.") from None
        _bump(conn)
    return _public(_as_dict(row))


def delete_rule(rule_id: int, user: str | None = None) -> None:
    """Delete a rule, keeping its row as history: it is never applied or listed again
    (unless deleted rules are asked for), and its key is free for a new rule."""
    db.require()
    from psycopg import sql

    with db.connection() as conn:
        removed = conn.execute(sql.SQL(
            "UPDATE {} SET is_deleted = true, deleted_at = now(), deleted_by = %s, modified_at = now() "
            "WHERE id = %s AND {}").format(_table(), sql.SQL(_LIVE)), [user, rule_id]).rowcount
        if not removed:
            raise not_found(f"Rule {rule_id}")
        _bump(conn)


def backfill_sql(conn) -> int:
    """The SQL of every live rule that has none yet (rules made before it was stored, or
    seeded by an older loader). Leaves modified_at alone: the rule itself did not change.
    Returns how many rules got their SQL."""
    from psycopg import sql

    rows = conn.execute(sql.SQL("SELECT {} FROM {} WHERE sql_text IS NULL AND {} AND kind = ANY(%s)").format(
        sql.SQL(_SELECT), _table(), sql.SQL(_LIVE)), [list(rule_sql.SQL_KINDS)]).fetchall()
    filled = 0
    for row in rows:
        data = _as_dict(row)
        try:
            text = sql_of(_rule(data))
        except (RuleError, TypeError):
            continue
        if text is not None:
            conn.execute(sql.SQL("UPDATE {} SET sql_text = %s, has_sql = true WHERE id = %s").format(_table()),
                         [text, data["id"]])
            filled += 1
    return filled


def _values(rule: Rule) -> dict:
    """Table column -> value, for the editable fields."""
    by_field = {field: column for column, field in _FIELDS}
    return {by_field[field]: getattr(rule, field) for field in _EDITABLE}


# --- seed files -------------------------------------------------------------------------------


def read_seeds(folder: Path = SEEDS) -> list[Rule]:
    """Every rule in the seed folder, validated; RuleError naming every bad file."""
    limits = allowed()
    rules, problems = [], []
    for path in sorted(folder.glob("*.yaml")):
        try:
            rules.extend(pc_rules.load_seed(path, limits))
        except RuleError as error:
            problems.append(str(error))
    if problems:
        raise RuleError("\n".join(problems))
    return rules


def load_seeds(conn, rules: list[Rule], force: bool = False, dry_run: bool = False) -> dict[str, list[str]]:
    """Insert the seed rules; update those still as seeded (or every one, with ``force``).
    A rule a person edited in the UI (approved_by is not 'seed') is kept as they left it."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    existing = {(r[0], r[1]): (r[2], _as_dict(r[3:])) for r in conn.execute(sql.SQL(
        "SELECT profit_center, rule_key, approved_by, {} FROM {} WHERE {}").format(
        sql.SQL(_SELECT), _table(), sql.SQL(_LIVE))).fetchall()}
    deleted = {(r[0], r[1]): r[2] for r in conn.execute(sql.SQL(
        "SELECT profit_center, rule_key, deleted_by FROM {} WHERE is_deleted").format(_table())).fetchall()}
    report: dict[str, list[str]] = {"added": [], "updated": [], "unchanged": [], "kept_edited": [],
                                    "kept_deleted": []}
    for rule in rules:
        values = _values(rule)
        text = sql_of(rule)
        name = f"{rule.profit_center} {rule.key}"
        current = existing.get((rule.profit_center, rule.key))
        if current is None and (rule.profit_center, rule.key) in deleted and not force:
            # Deleted in the UI: a person's decision, as an edit is.
            report["kept_deleted"].append(f"{name} (deleted by {deleted[(rule.profit_center, rule.key)] or 'someone'})")
            continue
        if current is None:
            report["added"].append(name)
            if not dry_run:
                conn.execute(sql.SQL("INSERT INTO {} ({}, sql_text, has_sql, approved_by) VALUES ({}, %s, %s, %s)")
                             .format(_table(), sql.SQL(", ").join(_ident(c) for c in values),
                                     sql.SQL(", ").join(sql.Placeholder() for _ in values)),
                             [Jsonb(v) if c == "spec" else v for c, v in values.items()]
                             + [text, text is not None, pc_rules.SEED_USER])
            continue
        edited_by, stored = current
        same = all(stored.get(field) == getattr(rule, field) for field in _EDITABLE)
        if same:
            report["unchanged"].append(name)
        elif edited_by != pc_rules.SEED_USER and not force:
            report["kept_edited"].append(f"{name} (edited by {edited_by})")
        else:
            report["updated"].append(name)
            if not dry_run:
                conn.execute(sql.SQL("UPDATE {} SET {}, sql_text = %s, has_sql = %s, approved_by = %s, "
                                     "approved_at = now(), modified_at = now() "
                                     "WHERE profit_center = %s AND rule_key = %s AND {}").format(
                    _table(), sql.SQL(", ").join(sql.SQL("{} = %s").format(_ident(c)) for c in values),
                    sql.SQL(_LIVE)),
                    [Jsonb(v) if c == "spec" else v for c, v in values.items()]
                    + [text, text is not None, pc_rules.SEED_USER, rule.profit_center, rule.key])
    if not dry_run:
        backfill_sql(conn)  # seeded before the SQL was stored
    if not dry_run and (report["added"] or report["updated"]):
        _bump(conn)
    return report


def export(conn, folder: Path) -> list[Path]:
    """Write the stored rules back to one YAML file per profit center."""
    import yaml
    from psycopg import sql

    rows = conn.execute(sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY profit_center, kind, seq, rule_key")
                        .format(sql.SQL(_SELECT), _table(), sql.SQL(_LIVE))).fetchall()
    by_pc: dict[str, list[dict]] = {}
    for row in rows:
        data = _as_dict(row)
        rule = {field: data[field] for field in _EDITABLE if field != "profit_center"
                and data[field] not in (None, "") and not (field == "enabled" and data[field] is True)
                and not (field == "seq" and data[field] == 100) and not (field == "target" and data[field] == "transaction")}
        by_pc.setdefault(data["profit_center"], []).append(rule)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for pc, items in by_pc.items():
        path = folder / f"{pc}.yaml"
        path.write_text(yaml.safe_dump({"profit_center": pc, "rules": items}, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
        written.append(path)
    return written
