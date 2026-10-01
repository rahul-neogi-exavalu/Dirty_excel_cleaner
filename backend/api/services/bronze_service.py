"""File -> Bronze ingestion: build a plan from cleaned jobs, let a person review it, run it.

The flow is human-in-the-loop by design:

1. ``create_plan`` reads the cleaned outputs of the chosen jobs, detects each file's
   source system (file-name suffix) and period (date columns, month sheets), and asks
   the planner (``ahi_bronze.planner``) what to do against the bronze layer as it is now.
2. The reviewer corrects source systems, periods, table names or actions; every change
   re-plans from scratch, so the plan shown is always the one that will run.
3. ``approve`` refuses until every blocker is fixed and every risky item (replace,
   schema evolution, separate table ...) is explicitly confirmed, then loads everything
   in one transaction: all of the plan is ingested, or none of it.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field

import polars as pl

from ahi_bronze import naming, periods, planner
from ahi_clean.orchestrate import SOURCE_SHEET_COLUMN
from ahi_bronze.periods import Period

from .. import config, db
from ..errors import ApiError, conflict, not_found
from ..store import SUCCEEDED, Job, OutputRecord, store

DRAFT = "draft"
RUNNING = "running"
SUCCEEDED_PLAN = "succeeded"
FAILED = "failed"

LINEAGE = ["_ingestion_id", "_source_file", "_source_sheet", "_ingested_at"]


@dataclass
class FileInput:
    job_id: str
    file_name: str
    file_sha256: str
    detected_source_system: str | None
    detected_period: Period | None
    period_source: str | None
    period_candidates: list[dict]
    source_system: str | None
    period: Period | None


@dataclass
class Plan:
    id: str
    batch_id: str | None
    files: list[FileInput]
    # item key -> {"table_name": ..., "action": ...} chosen by the reviewer
    overrides: dict[str, dict] = field(default_factory=dict)
    items: list[planner.PlanItem] = field(default_factory=list)
    status: str = DRAFT
    progress: float = 0.0
    message: str = ""
    error: dict | None = None
    reviewed_by: str | None = None
    results: dict[str, dict] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_plans: dict[str, Plan] = {}
_plans_lock = threading.Lock()


def _key(job: Job, record: OutputRecord) -> str:
    return f"{job.id}.{record.id}"


def _outputs(plan: Plan):
    for file in plan.files:
        job = store.job(file.job_id)
        for record in job.outputs:
            yield file, job, record


# --- reading the bronze layer ----------------------------------------------------


def _registry():
    """Tables and live ingestions, as the planner sees them."""
    from psycopg import sql

    control = sql.Identifier(config.CONTROL_SCHEMA)
    with db.connection() as conn:
        tables = [
            planner.TableState(name, [column["name"] for column in columns])
            for name, columns in conn.execute(
                sql.SQL("SELECT table_name, columns FROM {}.bronze_table").format(control))
        ]
        history = [
            planner.Ingested(row[0], row[1], row[2], row[3], Period.parse(row[4], row[5]), tuple(row[6] or ()))
            for row in conn.execute(sql.SQL(
                "SELECT id, table_name, file_name, file_sha256, period_start, period_end, source_sheets "
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
            "reviewed_by, created_at, superseded_by FROM {}.ingestion ORDER BY created_at DESC").format(control)).fetchall()
    by_table: dict[str, list[dict]] = {}
    for row in loads:
        by_table.setdefault(row[1], []).append({
            "id": row[0], "file_name": row[2], "period_start": row[3], "period_end": row[4],
            "action": row[5], "rows_loaded": row[6], "status": row[7], "reviewed_by": row[8],
            "created_at": row[9].timestamp(), "superseded_by": row[10],
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


# --- planning --------------------------------------------------------------------


def create_plan(job_ids: list[str], batch_id: str | None) -> Plan:
    db.require()
    files = []
    for job_id in dict.fromkeys(job_ids):
        job = store.job(job_id)
        if job.status != SUCCEEDED:
            raise conflict(f"{job.source_name} has not been cleaned successfully.",
                           "Only cleaned files can be ingested. Run the cleaning job again.")
        if not job.outputs:
            continue
        guess = _detect_period(job)
        source = naming.source_system_from_filename(job.source_name, config.SOURCE_SYSTEM_PATTERN)
        files.append(FileInput(
            job_id=job.id, file_name=job.source_name, file_sha256=job.source_sha256 or job.id,
            detected_source_system=source, detected_period=guess.period,
            period_source=guess.source, period_candidates=guess.candidates,
            source_system=source, period=guess.period,
        ))
    if not files:
        raise conflict("None of these files produced a table to ingest.")
    plan = Plan(id=uuid.uuid4().hex[:12], batch_id=batch_id, files=files)
    _replan(plan)
    with _plans_lock:
        _plans[plan.id] = plan
    return plan


def _detect_period(job: Job) -> periods.PeriodGuess:
    """One period per file: the span across all of its cleaned tables."""
    guesses = [periods.detect(record.frame, record.sheet_names) for record in job.outputs]
    found = [guess for guess in guesses if guess.period]
    candidates = [candidate for guess in guesses for candidate in guess.candidates]
    if not found:
        return periods.PeriodGuess(None, None, candidates)
    span = Period(min(g.period.start for g in found), max(g.period.end for g in found))
    return periods.PeriodGuess(span, found[0].source, candidates)


def get_plan(plan_id: str) -> Plan:
    with _plans_lock:
        found = _plans.get(plan_id)
    if found is None:
        raise not_found("That ingestion plan")
    return found


def _replan(plan: Plan) -> None:
    tables, history = _registry()
    candidates = []
    for file, job, record in _outputs(plan):
        key = _key(job, record)
        override = plan.overrides.get(key, {})
        candidates.append(planner.Candidate(
            key=key,
            file_name=file.file_name,
            file_sha256=file.file_sha256,
            source_system=file.source_system,
            sheet_names=list(record.sheet_names),
            columns=naming.column_names(record.columns),
            rows=record.frame.height,
            period=file.period,
            table_override=override.get("table_name"),
            action_override=override.get("action"),
        ))
    plan.items = planner.plan(candidates, tables, history)


def _editable(plan: Plan) -> None:
    if plan.status != DRAFT:
        raise conflict("This plan has already been approved.", "Start a new plan to make changes.")


def update_file(plan_id: str, job_id: str, source_system: str | None, period_start: str | None,
                period_end: str | None, fields: set[str]) -> Plan:
    plan = get_plan(plan_id)
    with plan.lock:
        _editable(plan)
        file = next((item for item in plan.files if item.job_id == job_id), None)
        if file is None:
            raise not_found("That file in the plan")
        if "source_system" in fields:
            file.source_system = naming.clean_source_system(source_system) or None
        if fields & {"period_start", "period_end"}:
            if period_start is None and period_end is None:
                file.period = None
            else:
                # One month given (either box) means a single-month period.
                parsed = Period.parse(period_start or period_end, period_end or period_start)
                if parsed is None:
                    raise ApiError(422, "invalid_period", "Enter the period as YYYY-MM.", field="period_start")
                file.period = parsed
        _replan(plan)
    return plan


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


def approve(plan_id: str, reviewed_by: str, confirmed: list[str]) -> Plan:
    plan = get_plan(plan_id)
    reviewer = reviewed_by.strip()
    if len(reviewer) < 2:
        raise ApiError(422, "reviewer_required", "Enter the reviewer's name.", field="reviewed_by")
    with plan.lock:
        _editable(plan)
        # The bronze layer may have changed since the plan was shown (another plan ran,
        # headers were renamed). Plan once more, and never run something the reviewer
        # did not see: confirmations were given for the plan as it was.
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
        plan.status, plan.reviewed_by = RUNNING, reviewer
        plan.started_at, plan.message = time.time(), "Starting"
    threading.Thread(target=_run, args=(plan,), name=f"ingest-{plan.id}", daemon=True).start()
    return plan


def _signature(items: list[planner.PlanItem]) -> list[tuple]:
    """What a reviewer approves: per item, where it goes, how, and what it removes."""
    return [
        (item.key, item.action, item.table_name, item.rebuild, tuple(item.columns_after),
         tuple(ref["id"] for ref in item.replaces))
        for item in items
    ]


def _run(plan: Plan) -> None:
    try:
        with db.connection() as conn:
            _execute(conn, plan)
        plan.status, plan.progress, plan.message = SUCCEEDED_PLAN, 1.0, "Ingested"
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
    finally:
        plan.finished_at = time.time()


def _execute(conn, plan: Plan) -> None:
    """Load the whole plan in the connection's single transaction."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    control = sql.Identifier(config.CONTROL_SCHEMA)
    bronze = sql.Identifier(config.BRONZE_SCHEMA)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('ahi-bronze'))")
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(bronze))
    conn.execute(
        sql.SQL("INSERT INTO {}.ingest_plan (id, batch_id, items, reviewed_by, status) "
                "VALUES (%s, %s, %s, %s, 'running')").format(control),
        [plan.id, plan.batch_id, Jsonb([item.as_dict() for item in plan.items]), plan.reviewed_by],
    )
    records = {_key(job, record): (file, job, record) for file, job, record in _outputs(plan)}
    work = [item for item in plan.items if item.action != planner.SKIP]

    for index, item in enumerate(plan.items):
        file, job, record = records[item.key]
        ingestion_id = uuid.uuid4().hex
        if item.action == planner.SKIP:
            _audit(conn, control, plan, item, file, job, record, ingestion_id, 0, "skipped")
            continue
        done = sum(1 for other in work[: work.index(item)])
        plan.progress = done / max(len(work), 1)
        plan.message = f"Loading {item.table_name}"
        rows = _load(conn, control, bronze, item, file, record, ingestion_id)
        if item.replaces:
            conn.execute(
                sql.SQL("UPDATE {}.ingestion SET status = 'superseded', superseded_by = %s "
                        "WHERE id = ANY(%s)").format(control),
                [ingestion_id, [ref["id"] for ref in item.replaces]],
            )
        _audit(conn, control, plan, item, file, job, record, ingestion_id, rows, "ingested")
        plan.results[item.key] = {"ingestion_id": ingestion_id, "rows_loaded": rows, "table_name": item.table_name}

    conn.execute(sql.SQL("UPDATE {}.ingest_plan SET status = 'succeeded', finished_at = now() "
                         "WHERE id = %s").format(control), [plan.id])


def _current_columns(conn, control, name: str) -> list[str] | None:
    from psycopg import sql

    row = conn.execute(sql.SQL("SELECT columns FROM {}.bronze_table WHERE table_name = %s FOR UPDATE")
                       .format(control), [name]).fetchone()
    return [column["name"] for column in row[0]] if row else None


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
        [config.BRONZE_SCHEMA, name]).fetchone() is not None


def _load(conn, control, bronze, item: planner.PlanItem, file: FileInput, record: OutputRecord,
          ingestion_id: str) -> int:
    from psycopg import sql
    from psycopg.types.json import Jsonb

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
    if item.rebuild or current is None:
        columns = [sql.SQL("{} text").format(sql.Identifier(name)) for name in item.columns_after]
        conn.execute(sql.SQL(
            "CREATE TABLE {}.{} ({}, _ingestion_id text NOT NULL, _source_file text NOT NULL, "
            "_source_sheet text, _ingested_at timestamptz NOT NULL DEFAULT now())"
        ).format(bronze, table, sql.SQL(", ").join(columns)))
        conn.execute(sql.SQL("CREATE INDEX ON {}.{} (_ingestion_id)").format(bronze, table))
    else:
        for name in [name for name in item.columns_after if name not in set(current)]:
            conn.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} text").format(bronze, table, sql.Identifier(name)))
        if item.replaces:
            conn.execute(sql.SQL("DELETE FROM {}.{} WHERE _ingestion_id = ANY(%s)").format(bronze, table),
                         [[ref["id"] for ref in item.replaces]])

    frame = _as_text(record)
    present = [name for name in item.columns_after if name in frame.columns]
    target = present + ["_ingestion_id", "_source_file", "_source_sheet"]
    sheets = list(dict.fromkeys(record.sheet_names))
    # The appended table's provenance column, under whatever name the reviewer gave it
    # (``_as_text`` keeps the column order, so its position identifies it).
    original = list(record.frame.columns)
    sheet_column = frame.columns[original.index(SOURCE_SHEET_COLUMN)] if SOURCE_SHEET_COLUMN in original else None
    copy_sql = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        bronze, table, sql.SQL(", ").join(sql.Identifier(name) for name in target))
    with conn.cursor() as cursor, cursor.copy(copy_sql) as copy:
        sheet_index = frame.columns.index(sheet_column) if sheet_column else None
        order = [frame.columns.index(name) for name in present]
        for row in frame.iter_rows():
            sheet = row[sheet_index] if sheet_index is not None else (sheets[0] if len(sheets) == 1 else None)
            copy.write_row([row[i] for i in order] + [ingestion_id, file.file_name, sheet])

    loaded = conn.execute(sql.SQL("SELECT count(*) FROM {}.{} WHERE _ingestion_id = %s").format(bronze, table),
                          [ingestion_id]).fetchone()[0]
    if loaded != record.frame.height:
        raise ApiError(500, "row_mismatch",
                       f"{item.table_name}: {loaded} rows written but the cleaned table has {record.frame.height}.",
                       "Nothing was ingested. Retry, and report it if it happens again.")

    dtypes = dict(zip(naming.column_names(record.columns), (str(d) for d in record.frame.dtypes)))
    previous = {}
    if current is not None and not item.rebuild:
        row = conn.execute(sql.SQL("SELECT columns FROM {}.bronze_table WHERE table_name = %s").format(control),
                           [item.table_name]).fetchone()
        previous = {column["name"]: column.get("datatype") for column in row[0]}
    described = [{"name": name, "datatype": dtypes.get(name) or previous.get(name)} for name in item.columns_after]
    conn.execute(
        sql.SQL(
            "INSERT INTO {}.bronze_table (table_name, schema_name, source_system, columns) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (table_name) DO UPDATE SET columns = EXCLUDED.columns, updated_at = now()"
        ).format(control),
        [item.table_name, config.BRONZE_SCHEMA, item.source_system, Jsonb(described)],
    )
    return loaded


def _as_text(record: OutputRecord) -> pl.DataFrame:
    """The cleaned table as the reviewer saw it -- renamed headers, values as text."""
    with record.lock:
        frame = record.frame.rename(record.renames) if record.renames else record.frame
    frame = frame.rename(dict(zip(frame.columns, naming.column_names(frame.columns))))
    columns = []
    for name, dtype in frame.schema.items():
        column = pl.col(name)
        if dtype == pl.Date:
            column = column.dt.to_string("%Y-%m-%d")
        elif isinstance(dtype, pl.Datetime):
            column = column.dt.to_string("%Y-%m-%d %H:%M:%S")
        columns.append(column.cast(pl.String).alias(name))
    return frame.select(columns)


def _audit(conn, control, plan: Plan, item: planner.PlanItem, file: FileInput, job: Job,
           record: OutputRecord, ingestion_id: str, rows: int, status: str) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    conn.execute(
        sql.SQL(
            "INSERT INTO {}.ingestion (id, plan_id, table_name, file_name, file_sha256, source_system, "
            "source_sheets, period_start, period_end, action, schema_diff, rows_loaded, status, reviewed_by, "
            "job_id, output_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        ).format(control),
        [ingestion_id, plan.id, item.table_name, file.file_name, file.file_sha256, item.source_system,
         list(record.sheet_names), item.period.start if item.period else None,
         item.period.end if item.period else None, item.action,
         Jsonb(item.comparison) if item.comparison else None, rows, status, plan.reviewed_by, job.id, record.id],
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
