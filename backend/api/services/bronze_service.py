"""Staged files -> Bronze: build a plan from the control table, let a person review it, run it.

The control table drives this step. Validate decided, per cleaned file, INSERT, APPEND or
REJECTED and staged the file's rows; here:

1. ``create_plan`` takes the control rows staged and not loaded yet (INSERT or APPEND),
   and asks the planner (``ahi_bronze.planner``) where each goes and how against the
   bronze layer as it is now: which table, append / reorder / evolve / a table of its
   own, and what the control table's decision replaces -- a year-to-date file supersedes
   the year's earlier loads, a monthly file may replace one month of them.
2. The reviewer may rename a table or choose another action; every change re-plans from
   scratch, so the plan shown is always the one that will run.
3. ``approve`` refuses until every blocker is fixed and every risky item (replace,
   schema evolution, separate table ...) is explicitly confirmed, then loads everything
   from staging in one transaction -- all of the plan, or none of it -- and marks each
   control row loaded (bronze_load_flag Y), and the rows it replaced inactive.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import polars as pl

from ahi_bronze import file_meta, naming, planner
from ahi_bronze import validation as rules
from ahi_bronze.periods import Period

from .. import config, db
from ..errors import ApiError, conflict, not_found
from ..store import OutputRecord
from . import job_history

DRAFT = "draft"
RUNNING = "running"
SUCCEEDED_PLAN = "succeeded"
FAILED = "failed"

LINEAGE = list(naming.LINEAGE_COLUMNS)
# Every bronze table carries these besides the file's own columns: pc_id first, the
# others after the file's columns (the business's bronze schema). Kept out of the
# registry's column list, so they never count as schema drift.
SYSTEM_TYPES = {"pc_id": "text", "file_received_date": "date", "reporting_start_date": "date",
                "reporting_end_date": "date", "division_name": "text", "file_name": "text",
                "processing_date": "timestamptz"}
CONTROL_TABLE = "control_table"


@dataclass
class StagedFile:
    """One control row staged by Validate and not loaded yet: what Ingest loads."""

    control_id: int
    file_name: str
    source_system: str  # the control table's: EXT_PC0796
    pc_id: str | None
    division_name: str | None
    file_received_date: date | None
    reporting_start_date: date
    reporting_end_date: date
    reporting_period_type: str | None
    date_detail: str | None
    processing_action: str
    file_replaced: str | None
    replace_month: str | None
    file_sha256: str
    sheet_names: list[str]
    staging_table: str
    job_id: str | None
    output_id: str | None
    staged_at: float | None
    # From the staging snapshot (control_table.validation).
    columns: list[dict] = field(default_factory=list)  # {name, header, datatype}, in file order
    provenance: str | None = None
    regions: list[str] = field(default_factory=list)
    rows: int = 0
    output_name: str | None = None
    fitness: int | None = None
    # Ingestions the control decision replaces (whole), and those it replaces a month of.
    replaces_ids: tuple[str, ...] = ()
    month_replace_ids: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return str(self.control_id)

    @property
    def bronze_source(self) -> str | None:
        """The bronze tables' source code: pc0796 (ext_pc0796_...)."""
        return file_meta.source_system_for(self.pc_id)

    @property
    def period(self) -> Period:
        return Period(rules.month_key(self.reporting_start_date), rules.month_key(self.reporting_end_date))


@dataclass
class Plan:
    id: str
    batch_id: str | None
    files: list[StagedFile]
    # item key -> {"table_name": ..., "action": ...} chosen by the reviewer
    overrides: dict[str, dict] = field(default_factory=dict)
    items: list[planner.PlanItem] = field(default_factory=list)
    status: str = DRAFT
    progress: float = 0.0
    message: str = ""
    error: dict | None = None
    reviewed_by: str | None = None
    # The signed-in user who approved it (user id), for the job history.
    approved_by: str | None = None
    results: dict[str, dict] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def file(self, key: str) -> StagedFile:
        return next(item for item in self.files if item.key == key)


_plans: dict[str, Plan] = {}
_plans_lock = threading.Lock()


def _control():
    from psycopg import sql

    return sql.SQL("{}.{}").format(sql.Identifier(config.CONTROL_SCHEMA), sql.Identifier(CONTROL_TABLE))


# --- reading the bronze layer ----------------------------------------------------


def _registry(conn=None):
    """Tables and live ingestions, as the planner sees them (on ``conn`` when given, so a
    load can re-plan inside its own locked transaction)."""
    if conn is None:
        with db.connection() as own:
            return _registry(own)
    from psycopg import sql

    control = sql.Identifier(config.CONTROL_SCHEMA)
    tables = [
        planner.TableState(
            name, [column["name"] for column in columns],
            headers={c["name"]: c["source_header"] for c in columns if c.get("source_header")},
            provenance=tuple(c["name"] for c in columns if c.get("provenance")),
        )
        for name, columns in conn.execute(
            sql.SQL("SELECT table_name, columns FROM {}.bronze_table").format(control))
    ]
    history = [
        planner.Ingested(row[0], row[1], row[2], row[3], Period.parse(row[4], row[5]), tuple(row[6] or ()),
                         tuple(row[7] or ()))
        for row in conn.execute(sql.SQL(
            "SELECT id, table_name, file_name, file_sha256, period_start, period_end, source_sheets, source_regions "
            "FROM {}.ingestion WHERE status = 'ingested'").format(control))
    ]
    return tables, history


def list_tables() -> list[dict]:
    from psycopg import sql

    control = sql.Identifier(config.CONTROL_SCHEMA)
    with db.connection() as conn:
        tables = conn.execute(sql.SQL(
            "SELECT table_name, schema_name, source_system, columns, created_at, updated_at "
            "FROM {}.bronze_table ORDER BY table_name").format(control)).fetchall()
        loads = conn.execute(sql.SQL(
            "SELECT id, table_name, file_name, period_start, period_end, action, rows_loaded, status, "
            "reviewed_by, created_at, superseded_by, pc_id, file_received_date, division_name, processing_date, "
            "control_id, reporting_start_date, reporting_end_date, reporting_period_type "
            "FROM {}.ingestion ORDER BY created_at DESC").format(control)).fetchall()
    by_table: dict[str, list[dict]] = {}
    for row in loads:
        by_table.setdefault(row[1], []).append({
            "id": row[0], "file_name": row[2], "period_start": row[3], "period_end": row[4],
            "action": row[5], "rows_loaded": row[6], "status": row[7], "reviewed_by": row[8],
            "created_at": row[9].timestamp(), "superseded_by": row[10], "pc_id": row[11],
            "file_received_date": row[12].isoformat() if row[12] else None, "division_name": row[13],
            "processing_date": row[14].timestamp() if row[14] else None, "control_id": row[15],
            "reporting_start_date": row[16].isoformat() if row[16] else None,
            "reporting_end_date": row[17].isoformat() if row[17] else None, "reporting_period_type": row[18],
        })
    result = []
    for name, schema, source, columns, created, updated in tables:
        history = by_table.get(name, [])
        live = [item for item in history if item["status"] == "ingested"]
        starts = [item["period_start"] for item in live if item["period_start"]]
        ends = [item["period_end"] for item in live if item["period_end"]]
        result.append({
            "table_name": name,
            "schema_name": schema,
            "source_system": source,
            "columns": columns,
            "rows": sum(item["rows_loaded"] for item in live),
            "period_start": min(starts) if starts else None,
            "period_end": max(ends) if ends else None,
            "created_at": created.timestamp(),
            "updated_at": updated.timestamp(),
            "ingestions": history,
        })
    return result


def upgrade_tables(conn) -> list[str]:
    """Bronze tables made before the control table: file_date becomes file_received_date,
    and the reporting dates and each row's month are added. Runs after the migrations,
    every start, and does nothing the second time."""
    from psycopg import sql

    if conn.execute("SELECT to_regclass(%s)", [f'"{config.CONTROL_SCHEMA}"."{CONTROL_TABLE}"']).fetchone()[0] is None:
        return []  # migration 006 not applied: nothing to upgrade to yet
    bronze = sql.Identifier(config.BRONZE_SCHEMA)
    changed = []
    names = [row[0] for row in conn.execute(sql.SQL("SELECT table_name FROM {}.bronze_table").format(
        sql.Identifier(config.CONTROL_SCHEMA)))]
    for name in names:
        columns = {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            [config.BRONZE_SCHEMA, name])}
        if not columns:
            continue
        table = sql.Identifier(name)
        if "file_date" in columns and "file_received_date" not in columns:
            conn.execute(sql.SQL("ALTER TABLE {}.{} RENAME COLUMN file_date TO file_received_date").format(bronze, table))
            changed.append(name)
        for column, kind in (("reporting_start_date", "date"), ("reporting_end_date", "date"),
                             ("_reporting_month", "text")):
            if column not in columns:
                conn.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} {}").format(
                    bronze, table, sql.Identifier(column), sql.SQL(kind)))
                if name not in changed:
                    changed.append(name)
    return changed


# --- what is staged ----------------------------------------------------------------


_STAGED_FIELDS = ("control_id", "file_name", "source_system", "pc_id", "division_name", "file_received_date",
                  "drt_reporting_start_date", "drt_reporting_end_date", "reporting_period_type", "date_detail",
                  "processing_action", "file_replaced", "replace_month", "file_sha256", "sheet_names",
                  "staging_table", "job_id", "output_id", "staged_at", "validation")


def _staged(conn, control_ids: list[int] | None) -> list[StagedFile]:
    """Control rows staged and not loaded yet (INSERT or APPEND): all, or these."""
    from psycopg import sql

    where = sql.SQL("bronze_load_flag = 'N' AND staging_table IS NOT NULL AND processing_action IN ('INSERT', 'APPEND') "
                    "AND drt_reporting_start_date IS NOT NULL AND drt_reporting_end_date IS NOT NULL")
    params: list = []
    if control_ids:
        where = sql.SQL("{} AND control_id = ANY(%s)").format(where)
        params.append(list(control_ids))
    rows = conn.execute(sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY control_id").format(
        sql.SQL(", ").join(sql.Identifier(name) for name in _STAGED_FIELDS), _control(), where), params).fetchall()
    files = [dict(zip(_STAGED_FIELDS, row)) for row in rows]
    # The ingestions behind the control rows a decision replaces.
    wanted = {cid for row in files for cid in ((row["validation"] or {}).get("replaces", [])
                                                + (row["validation"] or {}).get("overlaps", []))}
    ingestion = dict(conn.execute(sql.SQL("SELECT control_id, ingestion_id FROM {} WHERE control_id = ANY(%s) "
                                          "AND ingestion_id IS NOT NULL").format(_control()),
                                  [list(wanted)]).fetchall()) if wanted else {}
    staged = []
    for row in files:
        details = row.pop("validation") or {}
        replaces = tuple(ingestion[cid] for cid in details.get("replaces", []) if cid in ingestion)
        overlaps = tuple(ingestion[cid] for cid in details.get("overlaps", []) if cid in ingestion)
        staged.append(StagedFile(
            control_id=row["control_id"], file_name=row["file_name"], source_system=row["source_system"],
            pc_id=row["pc_id"], division_name=row["division_name"], file_received_date=row["file_received_date"],
            reporting_start_date=row["drt_reporting_start_date"], reporting_end_date=row["drt_reporting_end_date"],
            reporting_period_type=row["reporting_period_type"], date_detail=row["date_detail"],
            processing_action=row["processing_action"], file_replaced=row["file_replaced"],
            replace_month=row["replace_month"], file_sha256=row["file_sha256"] or f"control-{row['control_id']}",
            sheet_names=list(row["sheet_names"] or []), staging_table=row["staging_table"], job_id=row["job_id"],
            output_id=row["output_id"], staged_at=row["staged_at"].timestamp() if row["staged_at"] else None,
            columns=list(details.get("columns") or []), provenance=details.get("provenance"),
            regions=list(details.get("regions") or []), rows=int(details.get("rows") or 0),
            output_name=details.get("output_name"), fitness=details.get("fitness"),
            replaces_ids=replaces if row["processing_action"] == rules.INSERT else (),
            month_replace_ids=overlaps if row["replace_month"] else (),
        ))
    return staged


# --- planning --------------------------------------------------------------------


def create_plan(control_ids: list[int] | None, batch_id: str | None) -> Plan:
    db.require()
    with db.connection() as conn:
        files = _staged(conn, control_ids)
    if not files:
        raise conflict("Nothing is staged for Bronze.",
                       "Validate and stage cleaned files first; rejected files are never loaded.")
    plan = Plan(id=str(uuid.uuid4()), batch_id=batch_id, files=files)
    _replan(plan)
    with _plans_lock:
        _plans[plan.id] = plan
    return plan


def get_plan(plan_id: str) -> Plan:
    with _plans_lock:
        found = _plans.get(plan_id)
    if found is None:
        raise not_found("That ingestion plan")
    return found


def current_plan(plan_id: str) -> Plan:
    """The plan, re-planned when still a draft: the bronze layer may have moved on."""
    plan = get_plan(plan_id)
    with plan.lock:
        if plan.status == DRAFT:
            _replan(plan)
    return plan


def _replan(plan: Plan) -> None:
    plan.items = _planned(plan)


def _planned(plan: Plan, conn=None) -> list[planner.PlanItem]:
    """The plan's items against the bronze layer as it is now."""
    tables, history = _registry(conn)
    candidates = []
    for file in plan.files:
        override = plan.overrides.get(file.key, {})
        columns = [column["name"] for column in file.columns]
        candidates.append(planner.Candidate(
            key=file.key,
            file_name=file.file_name,
            file_sha256=file.file_sha256,
            source_system=file.bronze_source,
            sheet_names=list(file.sheet_names),
            columns=columns,
            rows=file.rows,
            period=file.period,
            table_override=override.get("table_name"),
            action_override=override.get("action"),
            regions=tuple(file.regions),
            headers={column["name"]: column["header"] for column in file.columns if column.get("header")},
            provenance=file.provenance,
            replaces_ids=file.replaces_ids,
            replace_month=file.replace_month,
            month_replace_ids=file.month_replace_ids,
        ))
    return planner.plan(candidates, tables, history)


def _editable(plan: Plan) -> None:
    if plan.status != DRAFT:
        raise conflict("This plan has already been approved.", "Start a new plan to make changes.")


def update_item(plan_id: str, key: str, table_name: str | None, action: str | None, fields: set[str]) -> Plan:
    plan = get_plan(plan_id)
    with plan.lock:
        _editable(plan)
        if not any(item.key == key for item in plan.items):
            raise not_found("That table in the plan")
        override = plan.overrides.setdefault(key, {})
        if "table_name" in fields:
            name = naming.identifier(table_name) if table_name and naming.slug(table_name) else None
            if name and not name.startswith(f"{naming.PREFIX}_"):
                raise ApiError(422, "invalid_table_name", "Bronze table names start with ext_.", field="table_name")
            override["table_name"] = name
        if "action" in fields:
            if action is not None and action not in planner.ACTIONS:
                raise ApiError(422, "invalid_action", f"Unknown action '{action}'.", field="action")
            override["action"] = action
        _replan(plan)
    return plan


def problems(plan: Plan, confirmed: set[str] | None = None) -> list[str]:
    return planner.approvable(plan.items, confirmed if confirmed is not None else {i.key for i in plan.items})


# --- running ---------------------------------------------------------------------


def approve(plan_id: str, reviewed_by: str, confirmed: list[str], user_id: str | None = None) -> Plan:
    plan = get_plan(plan_id)
    reviewer = reviewed_by.strip()
    if len(reviewer) < 2:
        raise ApiError(422, "reviewer_required", "Enter the reviewer's name.", field="reviewed_by")
    with plan.lock:
        _editable(plan)
        # The bronze layer may have changed since the plan was shown (another plan ran).
        # Plan once more, and never run something the reviewer did not see: confirmations
        # were given for the plan as it was.
        shown = _signature(plan.items)
        _replan(plan)
        if _signature(plan.items) != shown:
            raise ApiError(409, "plan_changed", "The plan changed since it was reviewed.",
                           "Review the updated plan and confirm again.")
        issues = planner.approvable(plan.items, set(confirmed))
        if issues:
            raise ApiError(409, "plan_not_ready", "The plan can't be approved yet.",
                           " ".join(issues[:3]) + (" …" if len(issues) > 3 else ""))
        if all(item.action == planner.SKIP for item in plan.items):
            raise conflict("Every table in this plan is skipped, so there is nothing to ingest.")
        plan.status, plan.reviewed_by, plan.approved_by = RUNNING, reviewer, user_id
        plan.started_at, plan.message = time.time(), "Starting"
    names = list(dict.fromkeys(file.file_name for file in plan.files))
    job_history.begin(
        job_history.BRONZE, plan.id, created_by=user_id, status=RUNNING, started_at=plan.started_at,
        batch_id=plan.batch_id, source_file=names[0] if len(names) == 1 else None,
        source_job_id=plan.files[0].job_id if len(plan.files) == 1 else None,
    )
    job_history.note(plan.id, f"Approved by {reviewer}: {len(plan.items)} table(s) from {len(names)} file(s)")
    for item in plan.items:
        job_history.note(plan.id, f"Plan: {item.action} -> {item.table_name} (control {item.key})")
    threading.Thread(target=_run, args=(plan,), name=f"ingest-{plan.id}", daemon=True).start()
    return plan


def _signature(items: list[planner.PlanItem]) -> list[tuple]:
    """What a reviewer approves: per item, where it goes, how, and what it removes."""
    return [
        (item.key, item.action, item.table_name, item.rebuild, tuple(item.columns_after),
         tuple(ref["id"] for ref in item.replaces), tuple(sorted(item.column_map.items())),
         item.replace_month, tuple(ref["id"] for ref in item.month_replaces))
        for item in items
    ]


def _run(plan: Plan) -> None:
    try:
        with db.connection() as conn:
            _execute(conn, plan)
        plan.status, plan.progress, plan.message = SUCCEEDED_PLAN, 1.0, "Ingested"
        plan.finished_at = time.time()
        job_history.finish(plan.id, "succeeded", _history_rows(plan), ended_at=plan.finished_at)
    except Exception as error:  # noqa: BLE001 - reported to the reviewer
        plan.status = FAILED
        plan.results = {}
        message = error.message if isinstance(error, ApiError) else "Ingestion failed; nothing was written."
        plan.error = {
            "message": message,
            "advice": error.advice if isinstance(error, ApiError) else
            "The whole plan was rolled back. Check the database and try again.",
            "technical": "".join(traceback.format_exception_only(type(error), error)).strip()[:2000],
        }
        plan.message = message
        _record_failure(plan)
        plan.finished_at = time.time()
        job_history.note(plan.id, message, "ERROR")
        job_history.note(plan.id, plan.error["technical"], "ERROR")
        job_history.finish(plan.id, "failed", ended_at=plan.finished_at)
    finally:
        plan.finished_at = plan.finished_at or time.time()


def _history_rows(plan: Plan) -> list[dict]:
    """One job history row per plan item: where each staged file went."""
    rows = []
    for item in plan.items:
        file = plan.file(item.key)
        rows.append({
            "status": "skipped" if item.action == planner.SKIP else "succeeded",
            "output_name": item.table_name, "output_file": file.staging_table,
            "row_count": plan.results.get(item.key, {}).get("rows_loaded", 0),
            "source_file": file.file_name, "source_sha256": file.file_sha256,
            "source_sheets": list(file.sheet_names), "source_job_id": file.job_id,
        })
    return rows


def _execute(conn, plan: Plan) -> None:
    """Load the whole plan in the connection's single transaction."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    control = sql.Identifier(config.CONTROL_SCHEMA)
    bronze = sql.Identifier(config.BRONZE_SCHEMA)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('ahi-bronze'))")
    # The authoritative check, under the lock: another plan may have loaded or replaced
    # files since this one was approved. Run only what the reviewer saw.
    pending = {row[0] for row in conn.execute(sql.SQL(
        "SELECT control_id FROM {} WHERE control_id = ANY(%s) AND bronze_load_flag = 'N' FOR UPDATE").format(_control()),
        [[file.control_id for file in plan.files]])}
    if pending != {file.control_id for file in plan.files}:
        raise ApiError(409, "plan_changed", "A file of this plan was loaded or changed in the meantime.",
                       "Start a new plan from the control table as it is now.")
    if _signature(_planned(plan, conn)) != _signature(plan.items):
        raise ApiError(409, "plan_changed", "The bronze layer changed since this plan was approved.",
                       "Start a new plan so it is checked against the tables as they are now.")
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(bronze))
    conn.execute(
        sql.SQL("INSERT INTO {}.ingest_plan (id, batch_id, items, reviewed_by, status) "
                "VALUES (%s, %s, %s, %s, 'running')").format(control),
        [plan.id, plan.batch_id, Jsonb([item.as_dict() for item in plan.items]), plan.reviewed_by],
    )
    work = [item for item in plan.items if item.action != planner.SKIP]
    stamp = None

    for item in plan.items:
        file = plan.file(item.key)
        ingestion_id = uuid.uuid4().hex
        if item.action == planner.SKIP:
            _audit(conn, control, plan, item, file, ingestion_id, 0, "skipped")
            # Nothing of it goes to Bronze: the control table says so, and why.
            conn.execute(sql.SQL(
                "UPDATE {} SET processing_action = 'REJECTED', is_active = 'N', rejection_reason = %s "
                "WHERE control_id = %s").format(_control()),
                [" ".join(item.reasons[-1:]) or "Skipped at ingestion.", file.control_id])
            job_history.note(plan.id, f"Skipped {file.file_name}")
            continue
        done = work.index(item)
        plan.progress = done / max(len(work), 1)
        plan.message = f"Loading {item.table_name}"
        # This load's processing_date. Taken per load (not now(), which is the same for
        # the whole transaction) and strictly increasing, so two loads never share one:
        # with the table and file name it identifies the load's rows in Silver.
        now = datetime.now(timezone.utc)
        stamp = now if stamp is None or now > stamp else stamp + timedelta(microseconds=1)
        rows = _load(conn, control, bronze, item, file, ingestion_id, stamp)
        if item.replaces:
            # Replaced loads in another table (a revised file that went to a table of its
            # own) lose their rows there; the target table's were handled by _load.
            elsewhere: dict[str, list[str]] = {}
            for ref in item.replaces:
                if ref.get("table_name") and ref["table_name"] != item.table_name:
                    elsewhere.setdefault(ref["table_name"], []).append(ref["id"])
            for other, ids in elsewhere.items():
                if _table_exists(conn, other):
                    conn.execute(sql.SQL("DELETE FROM {}.{} WHERE _ingestion_id = ANY(%s)").format(
                        bronze, sql.Identifier(other)), [ids])
            replaced = [ref["id"] for ref in item.replaces]
            superseded = conn.execute(
                sql.SQL("UPDATE {}.ingestion SET status = 'superseded', superseded_by = %s "
                        "WHERE id = ANY(%s) AND status = 'ingested'").format(control),
                [ingestion_id, replaced],
            ).rowcount
            if superseded != len(replaced):
                raise ApiError(409, "plan_changed", f"{item.table_name}: a file this plan replaces was already replaced.",
                               "Start a new plan so it is checked against the tables as they are now.")
            conn.execute(sql.SQL("UPDATE {} SET is_active = 'N' WHERE ingestion_id = ANY(%s)").format(_control()),
                         [replaced])
        if item.replace_month and item.month_replaces:
            _replace_month(conn, control, bronze, item)
        _audit(conn, control, plan, item, file, ingestion_id, rows, "ingested", stamp)
        conn.execute(sql.SQL(
            "UPDATE {} SET bronze_load_flag = 'Y', loaded_at = now(), ingestion_id = %s, bronze_table = %s "
            "WHERE control_id = %s").format(_control()), [ingestion_id, item.table_name, file.control_id])
        job_history.note(plan.id, f"Loaded {rows} rows from {file.file_name} into {item.table_name} ({item.action})")
        plan.results[item.key] = {"ingestion_id": ingestion_id, "rows_loaded": rows, "table_name": item.table_name,
                                  "control_id": file.control_id}

    conn.execute(sql.SQL("UPDATE {}.ingest_plan SET status = 'succeeded', finished_at = now() "
                         "WHERE id = %s").format(control), [plan.id])


def _replace_month(conn, control, bronze, item: planner.PlanItem) -> None:
    """Delete one month of the earlier loads holding it; they keep their other months,
    and Silver loads them again without it."""
    from psycopg import sql

    for ref in item.month_replaces:
        table = ref.get("table_name")
        if not table or not _table_exists(conn, table):
            continue
        removed = conn.execute(sql.SQL("DELETE FROM {}.{} WHERE _ingestion_id = %s AND _reporting_month = %s").format(
            bronze, sql.Identifier(table)), [ref["id"], item.replace_month]).rowcount
        conn.execute(sql.SQL("UPDATE {}.ingestion SET rows_loaded = greatest(rows_loaded - %s, 0), "
                             "silver_status = NULL WHERE id = %s").format(control), [removed, ref["id"]])


def _current_columns(conn, control, name: str) -> list[str] | None:
    from psycopg import sql

    row = conn.execute(sql.SQL("SELECT columns FROM {}.bronze_table WHERE table_name = %s FOR UPDATE")
                       .format(control), [name]).fetchone()
    return [column["name"] for column in row[0]] if row else None


def _provenance_columns(conn, control, name: str) -> list[str]:
    """The table's sheet-provenance columns, as the registry flags them."""
    from psycopg import sql

    row = conn.execute(sql.SQL("SELECT columns FROM {}.bronze_table WHERE table_name = %s").format(control),
                       [name]).fetchone()
    return [column["name"] for column in row[0] if column.get("provenance")] if row else []


def _table_exists(conn, name: str, schema: str | None = None) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
        [schema or config.BRONZE_SCHEMA, name]).fetchone() is not None


def _load(conn, control, bronze, item: planner.PlanItem, file: StagedFile, ingestion_id: str,
          processing_date: datetime) -> int:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    staging = sql.SQL("{}.{}").format(sql.Identifier(config.STAGING_SCHEMA), sql.Identifier(file.staging_table))
    if not _table_exists(conn, file.staging_table, config.STAGING_SCHEMA):
        raise conflict(f"{file.file_name} is no longer staged ({config.STAGING_SCHEMA}.{file.staging_table} is missing).",
                       "Validate and stage the file again.")
    table = sql.Identifier(item.table_name)
    current = _current_columns(conn, control, item.table_name)
    # The plan was approved against a picture of the table; refuse if it has moved.
    if not item.rebuild and current != item.columns_before and not (current is None and item.columns_before is None):
        raise conflict(f"{item.table_name} changed while the plan was being reviewed.",
                       "Start a new plan so it is checked against the table as it is now.")
    if item.columns_before is None and not item.rebuild and _table_exists(conn, item.table_name):
        raise conflict(f"A table named {item.table_name} already exists in {config.BRONZE_SCHEMA} "
                       "but is not in the registry.", "Choose another table name.")

    if item.rebuild:
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(bronze, table))

    def typed(name: str):
        return sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(SYSTEM_TYPES[name]))

    if item.rebuild or current is None:
        # The business's layout: pc_id, the file's columns, then the other file-level values.
        columns = ([typed("pc_id")] + [sql.SQL("{} text").format(sql.Identifier(name)) for name in item.columns_after]
                   + [typed(name) for name in naming.SYSTEM_COLUMNS if name != "pc_id"])
        conn.execute(sql.SQL(
            "CREATE TABLE {}.{} ({}, _ingestion_id text NOT NULL, _source_file text NOT NULL, "
            "_source_sheet text, _reporting_month text, _ingested_at timestamptz NOT NULL DEFAULT now())"
        ).format(bronze, table, sql.SQL(", ").join(columns)))
        conn.execute(sql.SQL("CREATE INDEX ON {}.{} (_ingestion_id)").format(bronze, table))
    else:
        # Tables made before these columns existed get them now (appended at the end).
        for name in naming.SYSTEM_COLUMNS:
            conn.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS {}").format(bronze, table, typed(name)))
        conn.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS _reporting_month text").format(bronze, table))
        for name in [name for name in item.columns_after if name not in set(current)]:
            conn.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} text").format(bronze, table, sql.Identifier(name)))
        if item.replaces:
            conn.execute(sql.SQL("DELETE FROM {}.{} WHERE _ingestion_id = ANY(%s)").format(bronze, table),
                         [[ref["id"] for ref in item.replaces]])

    # Staged column -> the table column it loads into (duplicate headers numbered in
    # another order load into the table's column for them).
    staged = [column["name"] for column in file.columns]
    into = {name: item.column_map.get(name, name) for name in staged}
    sheet_column = into.get(file.provenance) if file.provenance else None
    present = [(source, target) for source, target in into.items() if target in item.columns_after]
    # A one-sheet file appended to a table stacked from several sheets: the table's
    # sheet-provenance column gets this file's sheet name.
    provenance = [name for name in _provenance_columns(conn, control, item.table_name)
                  if name in item.columns_after and name not in into.values()]
    values = [file.pc_id, file.file_received_date, file.reporting_start_date, file.reporting_end_date,
              file.division_name, file.file_name, processing_date]
    targets = ([target for _, target in present] + provenance + list(naming.SYSTEM_COLUMNS)
               + ["_ingestion_id", "_source_file", "_source_sheet", "_reporting_month"])
    # Parameters in a SELECT list have no type of their own: cast each to its column's.
    sources = ([sql.Identifier(source) for source, _ in present] + [sql.Identifier("_source_sheet")] * len(provenance)
               + [sql.SQL("{}::{}").format(sql.Placeholder(), sql.SQL(SYSTEM_TYPES[name])) for name in naming.SYSTEM_COLUMNS]
               + [sql.SQL("%s::text"), sql.SQL("%s::text"), sql.Identifier("_source_sheet"),
                  sql.Identifier("_reporting_month")])
    conn.execute(sql.SQL("INSERT INTO {}.{} ({}) SELECT {} FROM {}").format(
        bronze, table, sql.SQL(", ").join(sql.Identifier(name) for name in targets),
        sql.SQL(", ").join(sources), staging), values + [ingestion_id, file.file_name])

    expected = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(staging)).fetchone()[0]
    loaded = conn.execute(sql.SQL("SELECT count(*) FROM {}.{} WHERE _ingestion_id = %s").format(bronze, table),
                          [ingestion_id]).fetchone()[0]
    if loaded != expected:
        raise ApiError(500, "row_mismatch",
                       f"{item.table_name}: {loaded} rows written but {expected} are staged.",
                       "Nothing was ingested. Retry, and report it if it happens again.")

    dtypes = {into[column["name"]]: column.get("datatype") for column in file.columns}
    headers = {into[column["name"]]: column.get("header") for column in file.columns if column.get("header")}
    previous = {}
    if current is not None and not item.rebuild:
        row = conn.execute(sql.SQL("SELECT columns FROM {}.bronze_table WHERE table_name = %s").format(control),
                           [item.table_name]).fetchone()
        previous = {column["name"]: column for column in row[0]}
    # Each column's type, its header as the source file wrote it (kept from earlier loads
    # when this file lacks the column), and whether it is the cleaner's sheet provenance.
    described = [
        {"name": name, "datatype": dtypes.get(name) or previous.get(name, {}).get("datatype"),
         "source_header": headers.get(name) or previous.get(name, {}).get("source_header"),
         "provenance": name == sheet_column or bool(previous.get(name, {}).get("provenance"))}
        for name in item.columns_after
    ]
    conn.execute(
        sql.SQL(
            "INSERT INTO {}.bronze_table (table_name, schema_name, source_system, columns) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (table_name) DO UPDATE SET columns = EXCLUDED.columns, updated_at = now()"
        ).format(control),
        [item.table_name, config.BRONZE_SCHEMA, item.source_system, Jsonb(described)],
    )
    return loaded


def ingested_columns(record: OutputRecord) -> list[tuple[str, str]]:
    """(original name, bronze column) for each column the reviewer kept, in table order.

    Bronze names come from the whole table, so leaving a column out never renames
    another (a headerless ``column_7`` stays ``column_7``).
    """
    names = naming.column_names(record.columns)
    return [(original, name) for original, name in zip(record.frame.columns, names)
            if original not in record.excluded]


def _as_text(record: OutputRecord) -> pl.DataFrame:
    """The cleaned table as the reviewer saw it -- renamed headers, left-out columns
    dropped, values as text."""
    with record.lock:
        kept = ingested_columns(record)
        frame = record.frame.select([pl.col(original).alias(name) for original, name in kept])
    columns = []
    for name, dtype in frame.schema.items():
        column = pl.col(name)
        if dtype == pl.Date:
            column = column.dt.to_string("%Y-%m-%d")
        elif isinstance(dtype, pl.Datetime):
            column = column.dt.to_string("%Y-%m-%d %H:%M:%S")
        columns.append(column.cast(pl.String).alias(name))
    return frame.select(columns)


def record_headers(record: OutputRecord) -> dict[str, str]:
    """Bronze column -> the header the source file wrote for it (columns the cleaner added,
    or that had no header, are absent)."""
    return {name: record.source_headers[original] for original, name in ingested_columns(record)
            if record.source_headers.get(original)}


def _audit(conn, control, plan: Plan, item: planner.PlanItem, file: StagedFile, ingestion_id: str, rows: int,
           status: str, processing_date: datetime | None = None) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    conn.execute(
        sql.SQL(
            "INSERT INTO {}.ingestion (id, plan_id, table_name, file_name, file_sha256, source_system, "
            "source_sheets, period_start, period_end, action, schema_diff, rows_loaded, status, reviewed_by, "
            "job_id, output_id, pc_id, file_received_date, division_name, processing_date, source_headers, "
            "source_regions, control_id, reporting_start_date, reporting_end_date, reporting_period_type) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s)"
        ).format(control),
        [ingestion_id, plan.id, item.table_name, file.file_name, file.file_sha256, item.source_system,
         list(file.sheet_names), file.period.start, file.period.end, item.action,
         Jsonb(item.comparison) if item.comparison else None, rows, status, plan.reviewed_by, file.job_id,
         file.output_id, file.pc_id, file.file_received_date, file.division_name, processing_date,
         Jsonb({column["name"]: column["header"] for column in file.columns if column.get("header")}),
         list(file.regions), file.control_id, file.reporting_start_date, file.reporting_end_date,
         file.reporting_period_type],
    )


def _record_failure(plan: Plan) -> None:
    """Keep a trace of the failed attempt; the load itself was rolled back."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    try:
        with db.connection() as conn:
            conn.execute(
                sql.SQL("INSERT INTO {}.ingest_plan (id, batch_id, items, reviewed_by, status, finished_at, error) "
                        "VALUES (%s, %s, %s, %s, 'failed', now(), %s) ON CONFLICT (id) DO UPDATE SET "
                        "status = 'failed', finished_at = now(), error = EXCLUDED.error")
                .format(sql.Identifier(config.CONTROL_SCHEMA)),
                [plan.id, plan.batch_id, Jsonb([item.as_dict() for item in plan.items]), plan.reviewed_by or "",
                 json.dumps(plan.error)],
            )
    except Exception:  # noqa: BLE001 - the original error is what matters
        pass
