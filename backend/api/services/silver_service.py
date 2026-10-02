"""Bronze -> Silver: pick eligible loads, review the whole column mapping, then load.

Human-in-the-loop, like Ingest:

1. ``eligible`` reads the ingestion audit table: loads that reached bronze and have no
   successful Silver run (the AHI document's rule).
2. ``create_run`` lets every matching method vote on every bronze column (saved,
   exact, fuzzy, word2vec, the AI -- independently, see ``ahi_silver.matching``),
   pre-selects the best-supported candidate and computes a dry-run quality report
   with the same code the load will use.
3. The reviewer picks any candidate (or any Silver column, or Ignore) for any row. ``approve`` refuses until every column is mapped or
   explicitly ignored, then -- in one transaction -- saves the approved mapping (and only
   then: suggestions are never saved), removes rows of superseded bronze loads, loads the
   cleansed rows into ``detail``, rebuilds ``summary`` and marks each load done.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field

import polars as pl

from ahi_silver import catalog as catalog_module
from ahi_silver import matching, normalize, profit_center, semantic, transform
from ahi_silver.catalog import SilverColumn
from ahi_silver.matching import Suggestion

from .. import config, db
from ..errors import ApiError, conflict, not_found
from . import job_history

DRAFT, RUNNING, SUCCEEDED, FAILED = "draft", "running", "succeeded", "failed"
SQL_TYPES = {"text": "text", "date": "date", "decimal": "numeric(18,2)"}
# How many bronze rows are read to pick sample values for the review.
SAMPLE_ROWS = 200


@dataclass
class Load:
    ingestion_id: str
    file_name: str
    rows: int
    period_start: str | None
    period_end: str | None
    # Lineage for the job history: the cleaning job and the file it came from.
    job_id: str | None = None
    file_sha256: str | None = None


@dataclass
class TableReview:
    table_name: str
    source_system: str | None
    pc_id: str | None
    loads: list[Load]
    columns: list[str]
    suggestions: list[Suggestion]
    quality: dict = field(default_factory=dict)
    # The selected loads' bronze rows, read once: every mapping edit re-checks quality
    # against them without going back to the database. Released when the run ends.
    frame: pl.DataFrame | None = field(default=None, repr=False)


@dataclass
class Run:
    id: str
    tables: list[TableReview]
    notes: list[str]
    cleanup: list[dict]
    lotl: profit_center.Lotl
    status: str = DRAFT
    progress: float = 0.0
    message: str = ""
    error: dict | None = None
    reviewed_by: str | None = None
    approved_by: str | None = None
    result: dict = field(default_factory=dict)
    # Silver rows written per bronze load (ingestion id), for the job history.
    loaded: dict[str, int] = field(default_factory=dict, repr=False)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_runs: dict[str, Run] = {}
_runs_lock = threading.Lock()


def _ident(name: str):
    from psycopg import sql

    return sql.Identifier(name)


def catalog() -> list[SilverColumn]:
    try:
        return catalog_module.load(config.SILVER_COLUMNS_FILE)
    except (OSError, ValueError) as error:
        raise ApiError(500, "silver_catalog", "The Silver column list could not be read.",
                       f"Check {config.SILVER_COLUMNS_FILE.name}.", detail=str(error)) from error


def _table_exists(conn, schema: str, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s)", [f'"{schema}"."{table}"']).fetchone()[0] is not None


def _lotl_ref() -> tuple[str, str]:
    schema, _, table = config.LOTL_TABLE.rpartition(".")
    return (schema or config.CONTROL_SCHEMA), table


# --- reading ---------------------------------------------------------------------


def eligible() -> dict:
    """Bronze loads that succeeded and have no successful Silver run, newest first."""
    from psycopg import sql

    with db.connection() as conn:
        rows = conn.execute(sql.SQL(
            "SELECT i.id, i.table_name, i.file_name, i.source_system, i.period_start, i.period_end, "
            "i.rows_loaded, i.created_at, i.silver_status FROM {}.ingestion i "
            "WHERE i.status = 'ingested' AND i.silver_status IS DISTINCT FROM 'succeeded' "
            "ORDER BY i.created_at DESC").format(_ident(config.CONTROL_SCHEMA))).fetchall()
        cleanup = _superseded(conn)
    return {
        "loads": [
            {"ingestion_id": r[0], "table_name": r[1], "file_name": r[2], "source_system": r[3],
             "period_start": r[4], "period_end": r[5], "rows": r[6], "ingested_at": r[7].timestamp(),
             "silver_status": r[8]}
            for r in rows
        ],
        "cleanup": cleanup,
    }


def _superseded(conn) -> list[dict]:
    """Loads replaced in bronze whose rows are still in Silver: removed by the next run."""
    from psycopg import sql

    rows = conn.execute(sql.SQL(
        "SELECT id, table_name, file_name, source_system FROM {}.ingestion "
        "WHERE status = 'superseded' AND silver_status = 'succeeded'").format(_ident(config.CONTROL_SCHEMA))).fetchall()
    return [{"ingestion_id": r[0], "table_name": r[1], "file_name": r[2],
             "pc_id": _pc_id(r[3])} for r in rows]


def _lotl(conn) -> profit_center.Lotl:
    from psycopg import sql

    schema, table = _lotl_ref()
    if not _table_exists(conn, schema, table):
        return profit_center.Lotl()
    rows = conn.execute(sql.SQL("SELECT pc_id, legacy_office_name, profit_center_number FROM {}.{}")
                        .format(_ident(schema), _ident(table))).fetchall()
    return profit_center.Lotl.from_rows(rows)


def _bronze_columns(conn, table: str) -> list[str]:
    from psycopg import sql

    row = conn.execute(sql.SQL("SELECT columns FROM {}.bronze_table WHERE table_name = %s")
                       .format(_ident(config.CONTROL_SCHEMA)), [table]).fetchone()
    if not row:
        raise conflict(f"{table} is not in the bronze registry.")
    return [column["name"] for column in row[0]]


def _bronze_frame(conn, table: str, columns: list[str], ingestion_ids: list[str], limit: int | None = None) -> pl.DataFrame:
    """Bronze rows (all text) for these loads, with their lineage columns."""
    from psycopg import sql

    wanted = columns + transform.LINEAGE
    query = sql.SQL("SELECT {} FROM {}.{} WHERE _ingestion_id = ANY(%s)").format(
        sql.SQL(", ").join(_ident(name) for name in wanted), _ident(config.BRONZE_SCHEMA), _ident(table))
    if limit:
        query = query + sql.SQL(" LIMIT {}").format(sql.Literal(limit))
    rows = conn.execute(query, [ingestion_ids]).fetchall()
    return pl.DataFrame(rows, schema={name: pl.String for name in wanted}, orient="row")


def _mapping_rows(conn) -> list[tuple]:
    """Every approved mapping row: (pc_id, bronze table, bronze column, silver column)."""
    from psycopg import sql

    schema = config.SILVER_SCHEMA
    if not _table_exists(conn, schema, "column_mapping"):
        return []
    return conn.execute(sql.SQL(
        "SELECT pc_id, bronze_table_name, bronze_column_name, silver_column_name FROM {}.column_mapping")
        .format(_ident(schema))).fetchall()


def _saved(rows: list[tuple], pc: str | None, table: str) -> tuple[dict[str, str | None], dict[str, str]]:
    """This table's approved mapping, and approved names elsewhere (by normalized name)."""
    own = {r[2]: r[3] for r in rows if r[0] == (pc or "") and r[1] == table}
    votes: dict[str, Counter] = {}
    for _, other_table, column, silver in rows:
        if silver and other_table != table:
            votes.setdefault(normalize.compact(column), Counter())[silver] += 1
    known = {name: counter.most_common(1)[0][0] for name, counter in votes.items()}
    return own, known


def _precedents(rows: list[tuple]) -> list[tuple[str, str | None]]:
    """Approved decisions worth showing the AI as examples: a renamed or abbreviated
    column (acc_eff_dt -> accounting_effective_date) or a deliberate ignore. A column
    named exactly like its Silver column teaches nothing."""
    pairs = {(r[2], r[3]) for r in rows if r[3] is None or normalize.compact(r[2]) != normalize.compact(r[3])}
    return sorted(pairs, key=lambda pair: (pair[0], pair[1] or ""))


def _samples(frame: pl.DataFrame, columns: list[str]) -> dict[str, list[str]]:
    result = {}
    for column in columns:
        if column in frame.columns:
            values = [value for value in frame[column].drop_nulls().unique(maintain_order=True).to_list() if str(value).strip()]
            result[column] = [str(value) for value in values[:3]]
    return result


def _matchers(precedents=()):
    similarity = None
    notes = []
    if config.WORD2VEC_PATH:
        try:
            similarity = semantic.load(config.WORD2VEC_PATH).similarity
        except OSError as error:
            notes.append(f"word2vec vectors could not be read: {error}")
    llm = None
    if config.AI_ENABLED and config.AI_PROVIDER == "azure_openai":
        from ahi_silver.llm import azure_openai_matcher

        llm = azure_openai_matcher(config.AZURE_OPENAI_API_KEY, config.AZURE_OPENAI_ENDPOINT,
                                   config.AZURE_OPENAI_API_VERSION, config.AZURE_OPENAI_DEPLOYMENT,
                                   config.AI_SEND_SAMPLES, precedents)
    elif config.AI_ENABLED and config.AI_PROVIDER == "gemini":
        from ahi_silver.llm import gemini_matcher

        llm = gemini_matcher(config.GEMINI_API_KEY, config.GEMINI_MODEL, config.AI_SEND_SAMPLES, precedents)
    return similarity, llm, notes


# --- planning --------------------------------------------------------------------


def _pc_id(source_system: str | None) -> str:
    """pc_id for a source: its numeric part (pc0515 -> 0515), or the code itself if it has none."""
    return profit_center.pc_id(source_system) or (source_system or "")


def create_run(ingestion_ids: list[str]) -> Run:
    db.require()
    from psycopg import sql

    columns_catalog = catalog()
    ids = list(dict.fromkeys(ingestion_ids))
    gathered = []
    # Read everything first, then let go of the connection: matching may call the AI or
    # load word2vec vectors, and neither should hold one of the pool's few connections.
    with db.connection() as conn:
        rows = conn.execute(sql.SQL(
            "SELECT id, table_name, file_name, source_system, period_start, period_end, rows_loaded, status, silver_status, "
            "job_id, file_sha256 FROM {}.ingestion WHERE id = ANY(%s)").format(_ident(config.CONTROL_SCHEMA)), [ids]).fetchall()
        found = {r[0]: r for r in rows}
        if any(i not in found for i in ids):
            raise not_found("A selected bronze load")
        not_ready = [found[i][2] for i in ids if found[i][7] != "ingested" or found[i][8] == "succeeded"]
        if not_ready:
            raise conflict(f"{', '.join(not_ready)} is not eligible for Silver.",
                           "Only bronze loads without a successful Silver run can be processed.")
        groups: dict[str, list] = {}
        for i in ids:
            groups.setdefault(found[i][1], []).append(found[i])
        approved = _mapping_rows(conn)
        for table, members in groups.items():
            source = members[0][3]
            pc = _pc_id(source)
            columns = _bronze_columns(conn, table)
            frame = _bronze_frame(conn, table, columns, [m[0] for m in members])
            own, known = _saved(approved, pc, table)
            gathered.append((table, members, source, pc, columns, frame, own, known))
        lotl = _lotl(conn)
        cleanup = _superseded(conn)
    if not ids and not cleanup:
        raise conflict("There is nothing to load or remove.", "Select at least one bronze load.")

    similarity, llm, notes = _matchers(_precedents(approved))
    tables = []
    for table, members, source, pc, columns, frame, own, known in gathered:
        suggestions, step_notes = matching.suggest(
            columns, columns_catalog, own, known, _samples(frame.head(SAMPLE_ROWS), columns),
            similarity=similarity, llm=llm, fuzzy_min=config.MATCH_FUZZY_MIN,
            semantic_min=config.MATCH_SEMANTIC_MIN,
        )
        notes.extend(note for note in step_notes if note not in notes)
        review = TableReview(
            table_name=table, source_system=source, pc_id=pc,
            loads=[Load(m[0], m[2], m[6], m[4], m[5], job_id=m[9], file_sha256=m[10]) for m in members],
            columns=columns, suggestions=suggestions, frame=frame,
        )
        _quality(review, columns_catalog, lotl)
        tables.append(review)

    run = Run(id=str(uuid.uuid4()), tables=tables, notes=notes, cleanup=cleanup, lotl=lotl)
    if lotl.empty:
        run.notes.append("LOTL not loaded: profit centers are kept as they are and flagged lotl_unavailable.")
    with _runs_lock:
        _runs[run.id] = run
    return run


def _mapping(review: TableReview) -> dict[str, str]:
    return {s.bronze_column: s.silver_column for s in review.suggestions if s.silver_column}


def _quality(review: TableReview, columns_catalog, lotl) -> None:
    """The dry run the reviewer sees: the load's own transform, on the rows read at review time."""
    _, quality = transform.transform(review.frame, _mapping(review), columns_catalog, review.pc_id, lotl)
    review.quality = quality


def get_run(run_id: str) -> Run:
    with _runs_lock:
        found = _runs.get(run_id)
    if found is None:
        raise not_found("That Silver run")
    return found


def problems(run: Run) -> list[str]:
    issues = []
    for review in run.tables:
        issues.extend(matching.problems(review.table_name, review.suggestions))
    return issues


def update_mapping(run_id: str, table: str, column: str, silver_column: str | None, ignored: bool) -> Run:
    run = get_run(run_id)
    columns_catalog = catalog()
    if silver_column is not None and silver_column not in {c.name for c in columns_catalog}:
        raise ApiError(422, "unknown_silver_column", f"'{silver_column}' is not a Silver column.", field="silver_column")
    with run.lock:
        if run.status != DRAFT:
            raise conflict("This run has already been approved.")
        review = next((t for t in run.tables if t.table_name == table), None)
        suggestion = next((s for s in (review.suggestions if review else []) if s.bronze_column == column), None)
        if suggestion is None:
            raise not_found("That column in the run")
        matching.choose(suggestion, silver_column, ignored)
        _quality(review, columns_catalog, run.lotl)
    return run


# --- loading ---------------------------------------------------------------------


def _still_valid(conn, run: Run, columns_catalog: list[SilverColumn]) -> None:
    """Refuse to load anything other than what the reviewer approved.

    Between review and approval another run may have loaded the same files, Bronze may
    have replaced one, a bronze table may have gained columns, or the Silver column list
    may have changed. Any of those makes the approved mapping stale.
    """
    from psycopg import sql

    names = {c.name for c in columns_catalog}
    for review in run.tables:
        for s in review.suggestions:
            if s.silver_column and s.silver_column not in names:
                raise ApiError(409, "plan_changed", f"Silver column {s.silver_column} no longer exists.",
                               "The Silver column list changed. Start a new Silver run.")
        if _bronze_columns(conn, review.table_name) != review.columns:
            raise ApiError(409, "plan_changed", f"{review.table_name} changed since the review.",
                           "Start a new Silver run.")
    ids = [load.ingestion_id for review in run.tables for load in review.loads]
    if ids:
        stale = conn.execute(sql.SQL(
            "SELECT file_name FROM {}.ingestion WHERE id = ANY(%s) AND "
            "(status <> 'ingested' OR silver_status IS NOT DISTINCT FROM 'succeeded')").format(
                _ident(config.CONTROL_SCHEMA)), [ids]).fetchall()
        if stale:
            raise ApiError(409, "plan_changed",
                           f"{', '.join(r[0] for r in stale)} was replaced in Bronze or already loaded to Silver.",
                           "Start a new Silver run.")


def approve(run_id: str, reviewed_by: str, user_id: str | None = None) -> Run:
    run = get_run(run_id)
    reviewer = reviewed_by.strip()
    if len(reviewer) < 2:
        raise ApiError(422, "reviewer_required", "Enter the reviewer's name.", field="reviewed_by")
    with run.lock:
        if run.status != DRAFT:
            raise conflict("This run has already been approved.")
        issues = problems(run)
        if issues:
            raise ApiError(409, "mapping_incomplete", "The mapping can't be approved yet.",
                           " ".join(issues[:3]) + (" …" if len(issues) > 3 else ""))
        # Fast feedback now; checked again under the lock when the load runs.
        with db.connection() as conn:
            _still_valid(conn, run, catalog())
        run.status, run.reviewed_by, run.started_at, run.message = RUNNING, reviewer, time.time(), "Starting"
        run.approved_by = user_id
    loads = [load for review in run.tables for load in review.loads]
    job_history.begin(
        job_history.SILVER, run.id, created_by=user_id, status=RUNNING, started_at=run.started_at,
        source_file=loads[0].file_name if len(loads) == 1 else None,
        source_job_id=loads[0].job_id if len(loads) == 1 else None,
    )
    job_history.note(run.id, f"Approved by {reviewer}: {len(loads)} bronze load(s) from {len(run.tables)} table(s)"
                     + (f", {len(run.cleanup)} replaced load(s) to remove" if run.cleanup else ""))
    threading.Thread(target=_run, args=(run,), name=f"silver-{run.id}", daemon=True).start()
    return run


def _run(run: Run) -> None:
    try:
        with db.connection() as conn:
            _execute(conn, run)
        run.status, run.progress, run.message = SUCCEEDED, 1.0, "Loaded"
        run.finished_at = time.time()
        job_history.note(run.id, f"Loaded {run.result.get('rows_loaded', 0)} rows, "
                                 f"removed {run.result.get('rows_removed', 0)}")
        job_history.finish(run.id, "succeeded", _history_rows(run), ended_at=run.finished_at)
    except Exception as error:  # noqa: BLE001 - reported to the reviewer
        run.status = FAILED
        message = error.message if isinstance(error, ApiError) else "The Silver load failed; nothing was written."
        run.error = {
            "message": message,
            "advice": error.advice if isinstance(error, ApiError) else "The run was rolled back. Check the database and try again.",
            "technical": "".join(traceback.format_exception_only(type(error), error)).strip()[:2000],
        }
        run.message = message
        _record_failure(run)
        run.finished_at = time.time()
        job_history.note(run.id, message, "ERROR")
        job_history.note(run.id, run.error["technical"], "ERROR")
        job_history.finish(run.id, "failed", ended_at=run.finished_at)
    finally:
        run.finished_at = run.finished_at or time.time()
        for review in run.tables:
            review.frame = None  # the run is over; its rows are no longer needed


def _history_rows(run: Run) -> list[dict]:
    """One job history row per bronze load the run moved into Silver."""
    target = f"{config.SILVER_SCHEMA}.detail"
    return [
        {"output_name": review.table_name, "output_file": target, "row_count": run.loaded.get(load.ingestion_id),
         "source_file": load.file_name, "source_sha256": load.file_sha256, "source_job_id": load.job_id}
        for review in run.tables for load in review.loads
    ]


def _ensure_tables(conn, columns_catalog: list[SilverColumn]) -> None:
    from psycopg import sql

    schema = _ident(config.SILVER_SCHEMA)
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
    conn.execute(sql.SQL(
        "CREATE TABLE IF NOT EXISTS {}.column_mapping (pc_id text NOT NULL, bronze_table_name text NOT NULL, "
        "bronze_column_name text NOT NULL, drt_column_name text, silver_column_name text, "
        "PRIMARY KEY (pc_id, bronze_table_name, bronze_column_name))").format(schema))
    typed = [sql.SQL("{} {}").format(_ident(c.name), sql.SQL(SQL_TYPES[c.data_type])) for c in columns_catalog]
    conn.execute(sql.SQL(
        "CREATE TABLE IF NOT EXISTS {}.detail ({}, pc_id text, pc_lookup_status text, business_key_hash text, "
        "row_hash text, _bronze_table text NOT NULL, _ingestion_id text NOT NULL, _source_file text, "
        "_source_sheet text, _silver_run_id text NOT NULL, _loaded_at timestamptz NOT NULL DEFAULT now())"
    ).format(schema, sql.SQL(", ").join(typed)))
    present = {row[0] for row in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'detail'",
        [config.SILVER_SCHEMA])}
    for column in columns_catalog:
        if column.name not in present:
            conn.execute(sql.SQL("ALTER TABLE {}.detail ADD COLUMN {} {}").format(
                schema, _ident(column.name), sql.SQL(SQL_TYPES[column.data_type])))
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS detail_ingestion_id ON {}.detail (_ingestion_id)").format(schema))
    conn.execute(sql.SQL(
        "CREATE TABLE IF NOT EXISTS {}.summary (pc_id text, profit_center_number text, profit_center_name text, "
        "accounting_month date, row_count integer NOT NULL, premium_total numeric(18,2), "
        "refreshed_at timestamptz NOT NULL DEFAULT now())").format(schema))


def _execute(conn, run: Run) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    columns_catalog = catalog()
    schema = _ident(config.SILVER_SCHEMA)
    control = _ident(config.CONTROL_SCHEMA)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('ahi-silver'))")
    # The authoritative check: under the lock no other Silver run can interleave.
    _still_valid(conn, run, columns_catalog)
    _ensure_tables(conn, columns_catalog)
    drt = {c.name: c.drt_name for c in columns_catalog}

    # 1. The approved mapping -- every row, including the pre-filled ones -- is saved.
    for review in run.tables:
        for s in review.suggestions:
            conn.execute(sql.SQL(
                "INSERT INTO {}.column_mapping (pc_id, bronze_table_name, bronze_column_name, drt_column_name, "
                "silver_column_name) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (pc_id, bronze_table_name, "
                "bronze_column_name) DO UPDATE SET drt_column_name = EXCLUDED.drt_column_name, "
                "silver_column_name = EXCLUDED.silver_column_name").format(schema),
                [review.pc_id or "", review.table_name, s.bronze_column, drt.get(s.silver_column) if s.silver_column else None,
                 s.silver_column])

    # 2. Rows of bronze loads that were replaced since they reached Silver.
    cleanup = _superseded(conn)
    removed = 0
    if cleanup:
        ids = [item["ingestion_id"] for item in cleanup]
        removed = conn.execute(sql.SQL("DELETE FROM {}.detail WHERE _ingestion_id = ANY(%s)").format(schema), [ids]).rowcount
        conn.execute(sql.SQL("UPDATE {}.ingestion SET silver_status = 'removed' WHERE id = ANY(%s)").format(control), [ids])

    # 3. Load each table's selected loads.
    lotl = _lotl(conn)
    names = [c.name for c in columns_catalog]
    extra = ["pc_id", "pc_lookup_status", "business_key_hash", "row_hash"]
    target = names + extra + ["_bronze_table", "_ingestion_id", "_source_file", "_source_sheet", "_silver_run_id"]
    copy_sql = sql.SQL("COPY {}.detail ({}) FROM STDIN").format(schema, sql.SQL(", ").join(_ident(n) for n in target))
    loaded, quality = 0, {}
    total = sum(len(review.loads) for review in run.tables) or 1
    done = 0
    for review in run.tables:
        ids = [load.ingestion_id for load in review.loads]
        # Idempotent: a load retried after a failure never doubles.
        conn.execute(sql.SQL("DELETE FROM {}.detail WHERE _ingestion_id = ANY(%s)").format(schema), [ids])
        frame = _bronze_frame(conn, review.table_name, review.columns, ids)
        silver, report = transform.transform(frame, _mapping(review), columns_catalog, review.pc_id, lotl)
        quality[review.table_name] = report
        run.message = f"Loading {review.table_name}"
        with conn.cursor() as cursor, cursor.copy(copy_sql) as copy:
            for row in silver.select(names + extra + transform.LINEAGE).iter_rows():
                body, lineage = row[: len(names) + len(extra)], row[len(names) + len(extra):]
                copy.write_row([*body, review.table_name, lineage[0], lineage[1], lineage[2], run.id])
        counts = dict(conn.execute(sql.SQL(
            "SELECT _ingestion_id, count(*) FROM {}.detail WHERE _ingestion_id = ANY(%s) GROUP BY 1").format(schema), [ids]).fetchall())
        expected = Counter(frame["_ingestion_id"].to_list())
        if any(counts.get(i, 0) != expected.get(i, 0) for i in ids):
            raise ApiError(500, "row_mismatch", f"{review.table_name}: Silver row counts do not match bronze.",
                           "Nothing was loaded. Retry, and report it if it happens again.")
        run.loaded.update({i: counts.get(i, 0) for i in ids})
        job_history.note(run.id, f"{review.table_name}: {silver.height} rows into {config.SILVER_SCHEMA}.detail")
        loaded += silver.height
        conn.execute(sql.SQL("UPDATE {}.ingestion SET silver_status = 'succeeded', silver_run_id = %s, "
                             "silver_loaded_at = now() WHERE id = ANY(%s)").format(control), [run.id, ids])
        done += len(ids)
        run.progress = done / total

    # 4. Summary, rebuilt for every profit center this run touched.
    pcs = sorted({review.pc_id or "" for review in run.tables} | {item["pc_id"] or "" for item in cleanup})
    _rebuild_summary(conn, columns_catalog, pcs)

    # The audit snapshot: what was approved, how it was chosen, and every method's vote.
    mapping = [
        {"pc_id": r.pc_id, "bronze_table_name": r.table_name, "bronze_column_name": s.bronze_column,
         "silver_column_name": s.silver_column, "ignored": s.ignored, "selection": s.selection,
         "methods": [v.method for v in s.backers()], "split": s.split,
         "votes": [{"method": v.method, "silver_column": v.silver_column, "score": v.score,
                    "second_choice": v.second_choice} for c in s.candidates for v in c.votes]}
        for r in run.tables for s in r.suggestions
    ]
    conn.execute(sql.SQL(
        "INSERT INTO {}.silver_run (id, ingestion_ids, reviewed_by, mapping, quality, rows_loaded, rows_removed, "
        "status, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'succeeded', now())").format(control),
        [run.id, [l.ingestion_id for r in run.tables for l in r.loads], run.reviewed_by, Jsonb(mapping),
         Jsonb(quality), loaded, removed])
    run.result = {"rows_loaded": loaded, "rows_removed": removed, "quality": quality}


def _rebuild_summary(conn, columns_catalog, pcs: list[str]) -> None:
    from psycopg import sql

    schema = _ident(config.SILVER_SCHEMA)
    names = {c.name for c in columns_catalog}

    def pick(name: str, cast: str) -> sql.Composable:
        return _ident(name) if name in names else sql.SQL(f"NULL::{cast}")

    month = (sql.SQL("date_trunc('month', {})::date").format(_ident("accounting_effective_date"))
             if "accounting_effective_date" in names else sql.SQL("NULL::date"))
    premium = sql.SQL("sum({})").format(_ident("premium")) if "premium" in names else sql.SQL("NULL::numeric")
    conn.execute(sql.SQL("DELETE FROM {}.summary WHERE pc_id = ANY(%s)").format(schema), [pcs])
    conn.execute(sql.SQL(
        "INSERT INTO {s}.summary (pc_id, profit_center_number, profit_center_name, accounting_month, row_count, "
        "premium_total) SELECT pc_id, {num}, {name}, {month}, count(*), {premium} FROM {s}.detail "
        "WHERE pc_id = ANY(%s) GROUP BY 1, 2, 3, 4").format(
            s=schema, num=pick("profit_center_number", "text"), name=pick("profit_center_name", "text"),
            month=month, premium=premium), [pcs])


def _record_failure(run: Run) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    try:
        with db.connection() as conn:
            conn.execute(sql.SQL(
                "INSERT INTO {}.silver_run (id, ingestion_ids, reviewed_by, mapping, status, error, finished_at) "
                "VALUES (%s, %s, %s, %s, 'failed', %s, now()) ON CONFLICT (id) DO UPDATE SET status = 'failed', "
                "error = EXCLUDED.error").format(_ident(config.CONTROL_SCHEMA)),
                [run.id, [l.ingestion_id for r in run.tables for l in r.loads], run.reviewed_by or "",
                 Jsonb([]), json.dumps(run.error)])
    except Exception:  # noqa: BLE001 - the original error is what matters
        pass


# --- browsing --------------------------------------------------------------------


def saved_mapping() -> list[dict]:
    from psycopg import sql

    with db.connection() as conn:
        if not _table_exists(conn, config.SILVER_SCHEMA, "column_mapping"):
            return []
        rows = conn.execute(sql.SQL(
            "SELECT pc_id, bronze_table_name, bronze_column_name, drt_column_name, silver_column_name "
            "FROM {}.column_mapping ORDER BY pc_id, bronze_table_name, bronze_column_name")
            .format(_ident(config.SILVER_SCHEMA))).fetchall()
    return [{"pc_id": r[0], "bronze_table_name": r[1], "bronze_column_name": r[2],
             "drt_column_name": r[3], "silver_column_name": r[4]} for r in rows]


def edit_saved_mapping(pc: str, table: str, column: str, silver_column: str | None) -> dict:
    """Change one approved mapping row; it applies from the next Silver run."""
    from psycopg import sql

    columns_catalog = {c.name: c for c in catalog()}
    if silver_column is not None and silver_column not in columns_catalog:
        raise ApiError(422, "unknown_silver_column", f"'{silver_column}' is not a Silver column.", field="silver_column")
    with db.connection() as conn:
        if not _table_exists(conn, config.SILVER_SCHEMA, "column_mapping"):
            raise not_found("That mapping row")
        updated = conn.execute(sql.SQL(
            "UPDATE {}.column_mapping SET silver_column_name = %s, drt_column_name = %s "
            "WHERE pc_id = %s AND bronze_table_name = %s AND bronze_column_name = %s").format(_ident(config.SILVER_SCHEMA)),
            [silver_column, columns_catalog[silver_column].drt_name if silver_column else None, pc, table, column]).rowcount
    if not updated:
        raise not_found("That mapping row")
    return {"pc_id": pc, "bronze_table_name": table, "bronze_column_name": column,
            "silver_column_name": silver_column}


def summary() -> list[dict]:
    from psycopg import sql

    with db.connection() as conn:
        if not _table_exists(conn, config.SILVER_SCHEMA, "summary"):
            return []
        rows = conn.execute(sql.SQL(
            "SELECT pc_id, profit_center_number, profit_center_name, accounting_month, row_count, premium_total "
            "FROM {}.summary ORDER BY pc_id, accounting_month").format(_ident(config.SILVER_SCHEMA))).fetchall()
    return [{"pc_id": r[0], "profit_center_number": r[1], "profit_center_name": r[2],
             "accounting_month": r[3].isoformat() if r[3] else None, "row_count": r[4],
             "premium_total": float(r[5]) if r[5] is not None else None} for r in rows]
