"""Bronze -> Silver: pick eligible loads, review the whole column mapping, then load.

Human-in-the-loop, like Ingest:

1. ``eligible`` reads the ingestion audit table: loads that reached bronze and have no
   successful Silver run (the AHI document's rule).
2. ``create_run`` lets every matching method vote on every bronze column (saved --
   the business's DRT column mapping for the profit center --, exact, fuzzy, word2vec,
   the AI; independently, see ``ahi_silver.matching``), pre-selects the best-supported
   candidate and computes a dry-run quality report with the same code the load will use.
3. The reviewer picks any candidate (or any Silver column, or Ignore) for any row, and
   can load one bronze column into more than one Silver column. ``approve`` refuses
   until every column is mapped or explicitly ignored, then -- in one transaction --
   saves the approved mappings into ``drt_column_mapping`` (only then, and never an
   Ignore), removes rows of superseded bronze loads, writes each load's cleansed rows
   into its source-specific Silver Cleansed table (``<cleansed schema>.<bronze table>``),
   loads them from there into Final Silver (``silver_detail``), rebuilds
   ``silver_aggregate`` and marks each load done.

A review lives in ``<control>.silver_draft`` from creation to the end of its load, so
every API process sees the same one and it survives a restart. Bronze rows are not stored
with it: they are read again (and kept briefly in memory) when the quality report is
recomputed.

``silver_detail`` and ``silver_aggregate`` have exactly the business's columns (see
``backend/config``). A bronze load's rows are identified in Silver by
(source_table, source_file, ingestion_timestamp = the load's processing_date), and in its
cleansed table by ``_ingestion_id``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

import polars as pl

from ahi_bronze import file_meta, naming
from ahi_silver import catalog as catalog_module
from ahi_silver import matching, normalize, profit_center, transform
from ahi_silver.catalog import MAPPED, SilverColumn
from ahi_silver.matching import Suggestion

from .. import config, db
from ..errors import ApiError, conflict, not_found
from . import job_history, reference_service
from .column_matching import header_key as _header_key  # noqa: F401 - shared with the Validate step
from .column_matching import matchers as _matchers
from .column_matching import precedents as _precedents
from .column_matching import same_pc as _same_pc
from .column_matching import samples as _samples
from .column_matching import saved_votes as _saved

log = logging.getLogger("ahi.silver")

DRAFT, RUNNING, SUCCEEDED, FAILED = "draft", "running", "succeeded", "failed"
DETAIL = "silver_detail"
AGGREGATE = "silver_aggregate"
DRT_TABLE = reference_service.DRT_TABLE
DRAFTS = "silver_draft"
CLEANSED_REGISTRY = "silver_cleansed_table"
# How many bronze rows are read to pick sample values for the review.
SAMPLE_ROWS = 200
# Bronze frames kept in memory for quality re-checks while a review is edited.
FRAME_CACHE = 4
# A running load that has not reported progress for this long, while no load holds the
# Silver lock, was interrupted (the process stopped).
STALE_RUNNING_MINUTES = 10
# Finished reviews are kept this long; silver_run keeps the audit for good.
FINISHED_KEEP_DAYS = 30
# What the cleansed table holds besides the Silver columns.
CLEANSED_EXTRA = [("_ingestion_id", "text NOT NULL"), ("_pc_status", "text"), ("_invalid_columns", "text[]"),
                  ("_silver_run_id", "text"), ("_cleansed_at", "timestamptz")]


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
    pc_id: str | None = None
    # The bronze load time: Silver's ingestion_timestamp and part of the load's identity.
    processing_date: datetime | None = None
    file_received_date: date | None = None
    division_name: str | None = None
    # The control table's reporting dates (whole months) and YTD / MONTHLY: Silver's drt_reporting_*.
    reporting_start_date: date | None = None
    reporting_end_date: date | None = None
    reporting_period_type: str | None = None


@dataclass
class TableReview:
    table_name: str
    source_system: str | None
    # The loads' main profit center (PC0796), and every one the loads carry.
    pc_id: str | None
    loads: list[Load]
    columns: list[str]
    suggestions: list[Suggestion]
    quality: dict = field(default_factory=dict)
    pc_ids: list[str] = field(default_factory=list)
    # Bronze column -> the header as the source file wrote it ("Net Premium").
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Run:
    id: str
    tables: list[TableReview]
    notes: list[str]
    cleanup: list[dict]
    lotl_rows: int = 0
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
    updated_at: float = field(default_factory=time.time)
    version: int = 1


def _ident(name: str):
    from psycopg import sql

    return sql.Identifier(name)


def catalog() -> list[SilverColumn]:
    """Every silver_detail column, in table order (mapped targets and system columns)."""
    try:
        return catalog_module.load(config.SILVER_COLUMNS_FILE)
    except (OSError, ValueError) as error:
        raise ApiError(500, "silver_catalog", "The Silver column list could not be read.",
                       f"Check {config.SILVER_COLUMNS_FILE.name}.", detail=str(error)) from error


def aggregate_catalog() -> list[SilverColumn]:
    try:
        return catalog_module.load_plain(config.SILVER_AGGREGATE_COLUMNS_FILE)
    except (OSError, ValueError) as error:
        raise ApiError(500, "silver_catalog", "The Silver aggregate column list could not be read.",
                       f"Check {config.SILVER_AGGREGATE_COLUMNS_FILE.name}.", detail=str(error)) from error


def _table_exists(conn, schema: str, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s)", [f'"{schema}"."{table}"']).fetchone()[0] is not None


def _source_table(table: str) -> str:
    """silver_detail.source_table: the bronze table, schema-qualified."""
    return f"{config.BRONZE_SCHEMA}.{table}"


# --- reading ---------------------------------------------------------------------


def eligible() -> dict:
    """Bronze loads that succeeded and have no successful Silver run, newest first."""
    from psycopg import sql

    with db.connection() as conn:
        rows = conn.execute(sql.SQL(
            "SELECT i.id, i.table_name, i.file_name, i.source_system, i.period_start, i.period_end, "
            "i.rows_loaded, i.created_at, i.silver_status, i.pc_id, i.division_name, i.file_received_date, "
            "coalesce(i.processing_date, i.created_at), i.reporting_start_date, i.reporting_end_date, "
            "i.reporting_period_type FROM {}.ingestion i "
            "WHERE i.status = 'ingested' AND i.silver_status IS DISTINCT FROM 'succeeded' "
            "ORDER BY i.created_at DESC").format(_ident(config.CONTROL_SCHEMA))).fetchall()
        cleanup = _superseded(conn)
    return {
        "loads": [
            {"ingestion_id": r[0], "table_name": r[1], "file_name": r[2], "source_system": r[3],
             "period_start": r[4], "period_end": r[5], "rows": r[6], "ingested_at": r[7].timestamp(),
             "silver_status": r[8], "pc_id": r[9] or file_meta.normalize_pc_id(r[3]), "division_name": r[10],
             "file_received_date": r[11].isoformat() if r[11] else None, "processing_date": r[12].timestamp(),
             "reporting_start_date": r[13].isoformat() if r[13] else None,
             "reporting_end_date": r[14].isoformat() if r[14] else None, "reporting_period_type": r[15]}
            for r in rows
        ],
        "cleanup": cleanup,
    }


def _superseded(conn) -> list[dict]:
    """Loads replaced in bronze whose rows are still in Silver: removed by the next run."""
    from psycopg import sql

    rows = conn.execute(sql.SQL(
        "SELECT id, table_name, file_name, source_system, pc_id, coalesce(processing_date, created_at) "
        "FROM {}.ingestion WHERE status = 'superseded' AND silver_status = 'succeeded'").format(
            _ident(config.CONTROL_SCHEMA))).fetchall()
    return [{"ingestion_id": r[0], "table_name": r[1], "file_name": r[2], "source_system": r[3],
             "pc_id": r[4] or file_meta.normalize_pc_id(r[3]), "processing_date": r[5].isoformat()} for r in rows]


def _lotl(conn) -> profit_center.Lotl:
    return profit_center.Lotl.from_rows(reference_service.lotl(conn))


def _registry(conn, table: str) -> list[dict]:
    """The bronze registry's columns for a table: {name, datatype, source_header, ...}."""
    from psycopg import sql

    row = conn.execute(sql.SQL("SELECT columns FROM {}.bronze_table WHERE table_name = %s")
                       .format(_ident(config.CONTROL_SCHEMA)), [table]).fetchone()
    if not row:
        raise conflict(f"{table} is not in the bronze registry.")
    return list(row[0])


def _bronze_columns(conn, table: str) -> list[str]:
    return [column["name"] for column in _registry(conn, table)]


def _bronze_headers(conn, table: str, ingestion_ids: list[str]) -> dict[str, str]:
    """Bronze column -> its source header: the registry's, then any the loads recorded."""
    from psycopg import sql

    headers = {c["name"]: c["source_header"] for c in _registry(conn, table) if c.get("source_header")}
    rows = conn.execute(sql.SQL("SELECT source_headers FROM {}.ingestion WHERE id = ANY(%s) ORDER BY created_at")
                        .format(_ident(config.CONTROL_SCHEMA)), [ingestion_ids]).fetchall()
    for (recorded,) in rows:
        for name, header in (recorded or {}).items():
            if header:
                headers.setdefault(name, header)
    return headers


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


# The selected loads' bronze rows, read once per process and reused for every quality
# re-check while a review is edited. Safe: an ``ingested`` load's rows never change.
_frames: OrderedDict[tuple, pl.DataFrame] = OrderedDict()
_frames_lock = threading.Lock()


def _frame(conn, review: TableReview) -> pl.DataFrame:
    ids = [load.ingestion_id for load in review.loads]
    key = (review.table_name, tuple(sorted(ids)), tuple(review.columns))
    with _frames_lock:
        if key in _frames:
            _frames.move_to_end(key)
            return _frames[key]
    frame = _bronze_frame(conn, review.table_name, review.columns, ids)
    with _frames_lock:
        _frames[key] = frame
        while len(_frames) > FRAME_CACHE:
            _frames.popitem(last=False)
    return frame


def _forget_frames(run: Run) -> None:
    keys = {(r.table_name, tuple(sorted(load.ingestion_id for load in r.loads)), tuple(r.columns)) for r in run.tables}
    with _frames_lock:
        for key in keys:
            _frames.pop(key, None)


def _mapping_rows(conn) -> list[tuple]:
    """Every DRT column mapping row: (profit_center, pc_column, drt_column, silver_column_name),
    in a fixed order, so the first of several rows is always the same one."""
    from psycopg import sql

    if not _table_exists(conn, config.SILVER_SCHEMA, DRT_TABLE):
        return []
    return conn.execute(sql.SQL(
        "SELECT profit_center, pc_column, drt_column, silver_column_name FROM {}.{} "
        "ORDER BY profit_center, pc_column, silver_column_name NULLS LAST, drt_column NULLS LAST")
        .format(_ident(config.SILVER_SCHEMA), _ident(DRT_TABLE))).fetchall()


# --- storing a review ------------------------------------------------------------


_LOAD_DATES = ("file_received_date", "reporting_start_date", "reporting_end_date")


def _load_state(load: Load) -> dict:
    data = dict(vars(load))
    data["processing_date"] = load.processing_date.isoformat() if load.processing_date else None
    for name in _LOAD_DATES:
        data[name] = data[name].isoformat() if data[name] else None
    return data


def _load_from(data: dict) -> Load:
    data = dict(data)
    # Reviews saved before the file received date had its name.
    if "file_date" in data:
        data.setdefault("file_received_date", data.pop("file_date"))
    data["processing_date"] = datetime.fromisoformat(data["processing_date"]) if data.get("processing_date") else None
    for name in _LOAD_DATES:
        data[name] = date.fromisoformat(data[name]) if data.get(name) else None
    return Load(**data)


def _state(run: Run) -> dict:
    """The whole review as JSON: what silver_draft.state holds."""
    return {
        "tables": [
            {"table_name": r.table_name, "source_system": r.source_system, "pc_id": r.pc_id, "pc_ids": r.pc_ids,
             "loads": [_load_state(load) for load in r.loads], "columns": r.columns, "headers": r.headers,
             "suggestions": [matching.suggestion_to_dict(s) for s in r.suggestions], "quality": r.quality}
            for r in run.tables
        ],
        "notes": run.notes,
        "cleanup": run.cleanup,
        "lotl_rows": run.lotl_rows,
        "loaded": run.loaded,
    }


def _stamp(value) -> float | None:
    return value.timestamp() if value else None


_DRAFT_SELECT = ("SELECT id, status, state, version, progress, message, error, result, reviewed_by, approved_by, "
                 "created_at, started_at, finished_at, updated_at, expires_at FROM {}.{} WHERE id = %s")


def _from_row(row) -> tuple[Run, datetime | None]:
    (run_id, status, state, version, progress, message, error, result, reviewed_by, approved_by,
     created_at, started_at, finished_at, updated_at, expires_at) = row
    tables = [
        TableReview(
            table_name=t["table_name"], source_system=t.get("source_system"), pc_id=t.get("pc_id"),
            loads=[_load_from(load) for load in t.get("loads", [])], columns=list(t.get("columns", [])),
            suggestions=[matching.suggestion_from_dict(s) for s in t.get("suggestions", [])],
            quality=t.get("quality") or {}, pc_ids=list(t.get("pc_ids") or []), headers=dict(t.get("headers") or {}),
        )
        for t in state.get("tables", [])
    ]
    run = Run(
        id=run_id, tables=tables, notes=list(state.get("notes", [])), cleanup=list(state.get("cleanup", [])),
        lotl_rows=int(state.get("lotl_rows") or 0), status=status, progress=float(progress or 0),
        message=message or "", error=error, reviewed_by=reviewed_by, approved_by=approved_by, result=result or {},
        loaded=dict(state.get("loaded") or {}), created_at=_stamp(created_at) or time.time(),
        started_at=_stamp(started_at), finished_at=_stamp(finished_at), updated_at=_stamp(updated_at) or time.time(),
        version=version,
    )
    return run, expires_at


def _read_run(conn, run_id: str, lock: bool = False) -> Run:
    from psycopg import sql

    query = _DRAFT_SELECT + (" FOR UPDATE" if lock else "")
    row = conn.execute(sql.SQL(query).format(_ident(config.CONTROL_SCHEMA), _ident(DRAFTS)), [run_id]).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "That Silver run was not found.",
                       "It may have expired or been removed. Start a new Silver run.")
    run, expires_at = _from_row(row)
    if run.status == DRAFT and expires_at is not None and expires_at < datetime.now(timezone.utc):
        raise ApiError(410, "run_expired", "This Silver review expired before it was approved.",
                       "Start a new Silver run; nothing was loaded.")
    return run


def _ttl():
    from datetime import timedelta

    return timedelta(hours=config.SILVER_DRAFT_TTL_HOURS)


def _insert_run(conn, run: Run) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    conn.execute(sql.SQL(
        "INSERT INTO {}.{} (id, ingestion_ids, status, state, version, progress, message, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s)").format(_ident(config.CONTROL_SCHEMA), _ident(DRAFTS)),
        [run.id, [load.ingestion_id for r in run.tables for load in r.loads], run.status, Jsonb(_state(run)),
         run.version, run.progress, run.message, _ttl()])


def _write_run(conn, run: Run) -> None:
    """Save the review; a draft's expiry starts again from now."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    def when(value):
        return datetime.fromtimestamp(value, timezone.utc) if value else None

    conn.execute(sql.SQL(
        "UPDATE {}.{} SET status = %s, state = %s, version = version + 1, progress = %s, message = %s, "
        "error = %s, result = %s, reviewed_by = %s, approved_by = %s, started_at = %s, finished_at = %s, "
        "updated_at = now(), expires_at = CASE WHEN %s = 'draft' THEN now() + %s ELSE expires_at END "
        "WHERE id = %s").format(_ident(config.CONTROL_SCHEMA), _ident(DRAFTS)),
        [run.status, Jsonb(_state(run)), run.progress, run.message, Jsonb(run.error) if run.error else None,
         Jsonb(_json_ready(run.result)) if run.result else None, run.reviewed_by, run.approved_by,
         when(run.started_at), when(run.finished_at), run.status, _ttl(), run.id])
    run.version += 1


def _json_ready(value):
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return _json(value)


def _progress(run: Run, progress: float, message: str) -> None:
    """Tell pollers how far the load is, on a connection of its own (the load's own
    transaction is not visible until it commits)."""
    from psycopg import sql

    run.progress, run.message = progress, message
    try:
        with db.connection() as conn:
            conn.execute(sql.SQL("UPDATE {}.{} SET progress = %s, message = %s, updated_at = now() WHERE id = %s")
                         .format(_ident(config.CONTROL_SCHEMA), _ident(DRAFTS)), [progress, message, run.id])
    except Exception:  # noqa: BLE001 - progress is a courtesy; the load goes on
        log.warning("Silver run %s: progress could not be saved", run.id, exc_info=True)


def _prune() -> None:
    """Drop expired drafts and old finished runs; mark loads that stopped mid-way as failed."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    drafts = sql.SQL("{}.{}").format(_ident(config.CONTROL_SCHEMA), _ident(DRAFTS))
    with db.connection() as conn:
        conn.execute(sql.SQL("DELETE FROM {} WHERE status = 'draft' AND expires_at < now()").format(drafts))
        conn.execute(sql.SQL("DELETE FROM {} WHERE status IN ('succeeded', 'failed') "
                             "AND coalesce(finished_at, updated_at) < now() - make_interval(days => %s)")
                     .format(drafts), [FINISHED_KEEP_DAYS])
        stale = conn.execute(sql.SQL("SELECT id FROM {} WHERE status = 'running' "
                                     "AND updated_at < now() - make_interval(mins => %s)")
                             .format(drafts), [STALE_RUNNING_MINUTES]).fetchall()
        # Only when no load holds the Silver lock: a slow load is not a stopped one.
        if stale and conn.execute("SELECT pg_try_advisory_xact_lock(hashtext('ahi-silver'))").fetchone()[0]:
            error = {"message": "The Silver load was interrupted before it finished; nothing was written.",
                     "advice": "Start a new Silver run.", "technical": "interrupted"}
            conn.execute(sql.SQL("UPDATE {} SET status = 'failed', error = %s, message = %s, finished_at = now(), "
                                 "updated_at = now() WHERE id = ANY(%s)").format(drafts),
                         [Jsonb(error), error["message"], [row[0] for row in stale]])


# --- planning --------------------------------------------------------------------


def _table_pc(members: list[tuple], source: str | None) -> tuple[str | None, list[str]]:
    """The profit center of a table's selected loads, and every distinct one seen."""
    seen = [pc for pc in (file_meta.normalize_pc_id(m[11]) for m in members) if pc]
    if not seen:
        fallback = file_meta.normalize_pc_id(source)
        return fallback, [fallback] if fallback else []
    return Counter(seen).most_common(1)[0][0], list(dict.fromkeys(seen))


def create_run(ingestion_ids: list[str]) -> Run:
    db.require()
    from psycopg import sql

    columns_catalog = catalog()
    ids = list(dict.fromkeys(ingestion_ids))
    gathered = []
    notes: list[str] = []
    _prune()
    # Read everything first, then let go of the connection: matching may call the AI or
    # load word2vec vectors, and neither should hold one of the pool's few connections.
    with db.connection() as conn:
        rows = conn.execute(sql.SQL(
            "SELECT id, table_name, file_name, source_system, period_start, period_end, rows_loaded, status, "
            "silver_status, job_id, file_sha256, pc_id, coalesce(processing_date, created_at), file_received_date, "
            "division_name, reporting_start_date, reporting_end_date, reporting_period_type "
            "FROM {}.ingestion WHERE id = ANY(%s)").format(_ident(config.CONTROL_SCHEMA)), [ids]).fetchall()
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
            pc, seen = _table_pc(members, source)
            if len(seen) > 1:
                notes.append(f"{table}: loads from {', '.join(seen)}. Each row keeps its own load's profit "
                             f"center; the approved mapping is saved for each of them.")
            review = TableReview(
                table_name=table, source_system=source, pc_id=pc, pc_ids=seen,
                loads=[Load(m[0], m[2], m[6], m[4], m[5], job_id=m[9], file_sha256=m[10],
                            pc_id=file_meta.normalize_pc_id(m[11]) or m[11], processing_date=m[12],
                            file_received_date=m[13], division_name=m[14], reporting_start_date=m[15],
                            reporting_end_date=m[16], reporting_period_type=m[17]) for m in members],
                columns=_bronze_columns(conn, table), suggestions=[],
            )
            review.headers = _bronze_headers(conn, table, [m[0] for m in members])
            frame = _frame(conn, review)
            own, known, one_to_many = _saved(approved, seen or ([pc] if pc else []), review.columns, review.headers)
            gathered.append((review, frame, own, known, one_to_many))
        lotl = _lotl(conn)
        cleanup = _superseded(conn)
    if not ids and not cleanup:
        raise conflict("There is nothing to load or remove.", "Select at least one bronze load.")

    first_pc = gathered[0][0].pc_id if gathered else None
    similarity, llm, matcher_notes = _matchers(_precedents(approved, first_pc))
    notes.extend(matcher_notes)
    tables = []
    for review, frame, own, known, one_to_many in gathered:
        suggestions, step_notes = matching.suggest(
            review.columns, columns_catalog, own, known, _samples(frame.head(SAMPLE_ROWS), review.columns),
            similarity=similarity, llm=llm, fuzzy_min=config.MATCH_FUZZY_MIN,
            semantic_min=config.MATCH_SEMANTIC_MIN, one_to_many=one_to_many,
        )
        notes.extend(note for note in step_notes if note not in notes)
        review.suggestions = suggestions
        _quality(review, columns_catalog, lotl, frame)
        tables.append(review)

    run = Run(id=str(uuid.uuid4()), tables=tables, notes=notes, cleanup=cleanup, lotl_rows=lotl.rows)
    if lotl.empty:
        run.notes.append("LOTL not loaded: profit centers are kept as they are and flagged lotl_unavailable.")
    with db.connection() as conn:
        _insert_run(conn, run)
    return run


def _mapping(review: TableReview) -> list[tuple[str, str]]:
    """(bronze column, Silver column) pairs; one bronze column may feed several."""
    return [(s.bronze_column, target) for s in review.suggestions for target in s.targets]


def _context(review: TableReview, processed_at: datetime | None = None) -> transform.Context:
    return transform.Context(
        source_system=review.source_system,
        source_table=_source_table(review.table_name),
        processed_at=processed_at,
        loads={load.ingestion_id: transform.LoadInfo(load.file_name, load.processing_date, load.period_start,
                                                      load.period_end, load.pc_id, load.reporting_start_date,
                                                      load.reporting_end_date, load.reporting_period_type)
               for load in review.loads},
    )


def _quality(review: TableReview, columns_catalog, lotl, frame: pl.DataFrame) -> None:
    """The dry run the reviewer sees: the load's own transform, on the loads' bronze rows."""
    _, quality = transform.transform(frame, _mapping(review), columns_catalog, review.pc_id, lotl, _context(review))
    review.quality = quality


def get_run(run_id: str) -> Run:
    with db.connection() as conn:
        run = _read_run(conn, run_id)
    if run.status == RUNNING and time.time() - run.updated_at > STALE_RUNNING_MINUTES * 60:
        _prune()  # the process running it may have stopped
        with db.connection() as conn:
            run = _read_run(conn, run_id)
    return run


def problems(run: Run) -> list[str]:
    issues = []
    for review in run.tables:
        issues.extend(matching.problems(review.table_name, review.suggestions))
    return issues


def update_mapping(run_id: str, table: str, column: str, silver_column: str | None, ignored: bool,
                   fields: set[str] | None = None, also: list[str] | None = None) -> Run:
    """The reviewer's choice for one column. ``fields``: which of silver_column / ignored /
    also were sent (all of the first two when not given). ``also``: the extra Silver
    columns the bronze column loads into, as a whole list."""
    columns_catalog = catalog()
    targets = {c.name for c in columns_catalog if c.role == MAPPED}
    fields = {"silver_column", "ignored"} if fields is None else fields
    for name in [silver_column, *(also or [])]:
        if name is not None and name not in targets:
            raise ApiError(422, "unknown_silver_column", f"'{name}' is not a Silver column a bronze column can map to.",
                           field="silver_column")
    with db.connection() as conn:
        run = _read_run(conn, run_id, lock=True)
        if run.status != DRAFT:
            raise conflict("This run has already been approved.")
        review = next((t for t in run.tables if t.table_name == table), None)
        suggestion = next((s for s in (review.suggestions if review else []) if s.bronze_column == column), None)
        if suggestion is None:
            raise not_found("That column in the run")
        if fields & {"silver_column", "ignored"}:
            matching.choose(suggestion, silver_column, ignored)
        if "also" in fields and also is not None:
            matching.set_also(suggestion, also)
        _quality(review, columns_catalog, _lotl(conn), _frame(conn, review))
        _write_run(conn, run)
    return run


def ignore_unmapped(run_id: str, table_name: str | None = None) -> Run:
    """Bulk-ignore every undecided column for a table, or for all tables at once.

    Avoids dozens of round trips when the reviewer wants to dismiss every column
    no method could match. Each column is set exactly as ``choose(… ignored=True)``
    would, and each changed table's quality report is rebuilt once. Ignores are never
    saved to the DRT column mapping.
    """
    columns_catalog = catalog()
    with db.connection() as conn:
        run = _read_run(conn, run_id, lock=True)
        if run.status != DRAFT:
            raise conflict("This run has already been approved.")
        reviews = run.tables if table_name is None else [t for t in run.tables if t.table_name == table_name]
        if table_name is not None and not reviews:
            raise not_found("That table in the run")
        lotl = None
        for review in reviews:
            changed = 0
            for suggestion in review.suggestions:
                if not suggestion.decided:
                    matching.choose(suggestion, None, True)
                    changed += 1
            if changed:
                lotl = lotl or _lotl(conn)
                _quality(review, columns_catalog, lotl, _frame(conn, review))
        _write_run(conn, run)
    return run


# --- loading ---------------------------------------------------------------------


def _still_valid(conn, run: Run, columns_catalog: list[SilverColumn]) -> None:
    """Refuse to load anything other than what the reviewer approved.

    Between review and approval another run may have loaded the same files, Bronze may
    have replaced one, a bronze table may have gained columns, or the Silver column list
    may have changed. Any of those makes the approved mapping stale.
    """
    from psycopg import sql

    names = {c.name for c in columns_catalog if c.role == MAPPED}
    for review in run.tables:
        for s in review.suggestions:
            for target in s.targets:
                if target not in names:
                    raise ApiError(409, "plan_changed", f"Silver column {target} no longer exists.",
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
    reviewer = reviewed_by.strip()
    if len(reviewer) < 2:
        raise ApiError(422, "reviewer_required", "Enter the reviewer's name.", field="reviewed_by")
    with db.connection() as conn:
        run = _read_run(conn, run_id, lock=True)
        if run.status != DRAFT:
            raise conflict("This run has already been approved.")
        issues = problems(run)
        if issues:
            raise ApiError(409, "mapping_incomplete", "The mapping can't be approved yet.",
                           " ".join(issues[:3]) + (" …" if len(issues) > 3 else ""))
        # Fast feedback now; checked again under the lock when the load runs.
        _still_valid(conn, run, catalog())
        run.status, run.reviewed_by, run.started_at, run.message = RUNNING, reviewer, time.time(), "Starting"
        run.approved_by = user_id
        _write_run(conn, run)
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
        run.finished_at = time.time()
        _record_failure(run)
        job_history.note(run.id, message, "ERROR")
        job_history.note(run.id, run.error["technical"], "ERROR")
        job_history.finish(run.id, "failed", ended_at=run.finished_at)
    finally:
        _forget_frames(run)  # the run is over; its rows are no longer needed


def _history_rows(run: Run) -> list[dict]:
    """One job history row per bronze load the run moved into Silver."""
    target = f"{config.SILVER_SCHEMA}.{DETAIL}"
    return [
        {"output_name": review.table_name, "output_file": target, "row_count": run.loaded.get(load.ingestion_id),
         "source_file": load.file_name, "source_sha256": load.file_sha256, "source_job_id": load.job_id}
        for review in run.tables for load in review.loads
    ]


def _column_sql(name: str, type_sql: str):
    from psycopg import sql

    return sql.SQL("{} {}").format(_ident(name), sql.SQL(type_sql))


def _ensure_table(conn, schema_name: str, table: str, columns: list[SilverColumn], identity: str,
                  extra: list[tuple[str, str]] = ()) -> None:
    """Create a table with exactly these columns (then ``extra``: (name, SQL type) pairs),
    or add the ones it lacks."""
    from psycopg import sql

    def definition(column: SilverColumn, generated: bool):
        return _column_sql(column.name, column.sql_type + (" GENERATED BY DEFAULT AS IDENTITY" if generated else ""))

    schema = _ident(schema_name)
    wanted = [definition(c, c.name == identity) for c in columns] + [_column_sql(n, t) for n, t in extra]
    conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(schema, _ident(table), sql.SQL(", ").join(wanted)))
    present = {row[0]: row[1] for row in conn.execute(
        "SELECT column_name, numeric_precision FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        [schema_name, table])}
    for column in columns:
        if column.name not in present:
            conn.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN {}").format(schema, _ident(table), definition(column, False)))
        elif column.kind == "decimal" and present[column.name] and present[column.name] < column.precision:
            # Widen a numeric column whose catalog precision grew (e.g. aggregate 18 -> 28).
            conn.execute(sql.SQL("ALTER TABLE {}.{} ALTER COLUMN {} TYPE {}").format(
                schema, _ident(table), _ident(column.name),
                sql.SQL(column.sql_type)))
    for name, type_sql in extra:
        if name not in present:
            # Added to a table with rows: a NOT NULL extra starts out nullable.
            conn.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN {}").format(
                schema, _ident(table), _column_sql(name, type_sql.replace(" NOT NULL", ""))))


def _ensure_tables(conn, columns_catalog: list[SilverColumn], aggregate_columns: list[SilverColumn]) -> None:
    from psycopg import sql

    schema = _ident(config.SILVER_SCHEMA)
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
    _ensure_table(conn, config.SILVER_SCHEMA, DETAIL, columns_catalog, transform.IDENTITY)
    # A bronze load's rows: what replacing or reloading it deletes.
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS silver_detail_load ON {}.{} "
                         "(source_table, source_file, ingestion_timestamp)").format(schema, _ident(DETAIL)))
    _ensure_table(conn, config.SILVER_SCHEMA, AGGREGATE, aggregate_columns, "ahi_aggregate_id")
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS silver_aggregate_source ON {}.{} (source_system)")
                 .format(schema, _ident(AGGREGATE)))


def _cleansed_columns(columns_catalog: list[SilverColumn]) -> list[SilverColumn]:
    """The Silver columns a cleansed table holds: all of them but the generated key."""
    return [column for column in columns_catalog if column.name != transform.IDENTITY]


def _ensure_cleansed(conn, table: str, columns_catalog: list[SilverColumn]) -> None:
    """The bronze table's source-specific Silver Cleansed table: the same name, in the
    cleansed schema, with the Silver columns and the load each row came from."""
    from psycopg import sql

    schema = _ident(config.CLEANSED_SCHEMA)
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
    _ensure_table(conn, config.CLEANSED_SCHEMA, table, _cleansed_columns(columns_catalog), "", CLEANSED_EXTRA)
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (_ingestion_id)").format(
        _ident(naming.identifier(f"{table}_load")), schema, _ident(table)))
    conn.execute(sql.SQL(
        "INSERT INTO {}.{} (bronze_table, cleansed_table) VALUES (%s, %s) "
        "ON CONFLICT (bronze_table) DO UPDATE SET updated_at = now()").format(
            _ident(config.CONTROL_SCHEMA), _ident(CLEANSED_REGISTRY)),
        [table, f"{config.CLEANSED_SCHEMA}.{table}"])


def _delete_cleansed(conn, table: str, ingestion_ids: list[str]) -> int:
    from psycopg import sql

    if not ingestion_ids or not _table_exists(conn, config.CLEANSED_SCHEMA, table):
        return 0
    return conn.execute(sql.SQL("DELETE FROM {}.{} WHERE _ingestion_id = ANY(%s)").format(
        _ident(config.CLEANSED_SCHEMA), _ident(table)), [ingestion_ids]).rowcount


def _delete_load(conn, source_table: str, file_name: str, processing_date) -> int:
    from psycopg import sql

    return conn.execute(sql.SQL(
        "DELETE FROM {}.{} WHERE source_table = %s AND source_file = %s AND ingestion_timestamp = %s").format(
            _ident(config.SILVER_SCHEMA), _ident(DETAIL)), [source_table, file_name, processing_date]).rowcount


def _current_rows(rows: list[tuple], header: str) -> list[tuple]:
    """The mapping rows that are this source column: the same header as written (spaces
    and case aside); failing that, rows whose header has the same words -- but only when
    they are all one header, so 'Agent Commission' never stands for 'Agent Commission%'."""
    exact = [row for row in rows if _header_key(row[1]) == _header_key(header)]
    if exact:
        return exact
    alike = [row for row in rows if normalize.compact(row[1] or "") == normalize.compact(header)]
    return alike if len({_header_key(row[1]) for row in alike}) == 1 else []


def _save_mapping(conn, run: Run, columns_catalog: list[SilverColumn]) -> int:
    """Save every approved mapping into the DRT column mapping, for each profit center the
    table's loads carry. Returns the rows added.

    A source column's rows become exactly its approved targets: rows for other Silver
    columns go, missing ones are added under the header the file wrote. drt_column is the
    business's DRT label for the target, or NULL when the business has none for it. An
    ignored (or undecided) column saves nothing and removes nothing: it is asked about
    again next time. Rows naming a DRT label with no Silver column are left alone.
    """
    from psycopg import sql

    table = sql.SQL("{}.{}").format(_ident(config.SILVER_SCHEMA), _ident(DRT_TABLE))
    labels = reference_service.drt_labels(conn, columns_catalog)
    saved = 0
    for review in run.tables:
        for pc in review.pc_ids or ([review.pc_id] if review.pc_id else []):
            mine = [row for row in _mapping_rows(conn) if _same_pc(row[0], pc) and row[3]]
            for s in review.suggestions:
                wanted = s.targets
                if not wanted:
                    continue
                header = review.headers.get(s.bronze_column) or s.bronze_column
                current = _current_rows(mine, header)
                for profit_center_code, pc_column, drt_column, silver in current:
                    if silver not in wanted:
                        conn.execute(sql.SQL(
                            "DELETE FROM {} WHERE profit_center = %s AND pc_column = %s AND "
                            "silver_column_name IS NOT DISTINCT FROM %s AND drt_column IS NOT DISTINCT FROM %s").format(table),
                            [profit_center_code, pc_column, silver, drt_column])
                have = {row[3] for row in current}
                code = current[0][0] if current else pc
                written = current[0][1] if current else header
                for target in wanted:
                    if target in have:
                        continue
                    saved += conn.execute(sql.SQL(
                        "INSERT INTO {} (profit_center, pc_column, drt_column, silver_column_name) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING").format(table),
                        [code, written, labels.get(target), target]).rowcount
    return saved


def _execute(conn, run: Run) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    columns_catalog = catalog()
    aggregate_columns = aggregate_catalog()
    control = _ident(config.CONTROL_SCHEMA)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('ahi-silver'))")
    # The authoritative check: under the lock no other Silver run can interleave.
    _still_valid(conn, run, columns_catalog)
    _ensure_tables(conn, columns_catalog, aggregate_columns)

    # 1. The approved mapping -- every mapped column, including the pre-filled ones -- is saved.
    saved = _save_mapping(conn, run, columns_catalog)

    # 2. Rows of bronze loads that were replaced since they reached Silver: from Final
    #    Silver and from the cleansed table they went through.
    cleanup = _superseded(conn)
    removed = 0
    if cleanup:
        for item in cleanup:
            removed += _delete_load(conn, _source_table(item["table_name"]), item["file_name"],
                                    datetime.fromisoformat(item["processing_date"]))
            _delete_cleansed(conn, item["table_name"], [item["ingestion_id"]])
        conn.execute(sql.SQL("UPDATE {}.ingestion SET silver_status = 'removed' WHERE id = ANY(%s)").format(control),
                     [[item["ingestion_id"] for item in cleanup]])

    # 3. Each table's selected loads: bronze -> Silver Cleansed -> Final Silver.
    lotl = _lotl(conn)
    processed_at = datetime.now(timezone.utc)
    target = [c.name for c in _cleansed_columns(columns_catalog)]
    names = sql.SQL(", ").join(_ident(n) for n in target)
    detail = sql.SQL("{}.{}").format(_ident(config.SILVER_SCHEMA), _ident(DETAIL))
    loaded, quality = 0, {}
    total = sum(len(review.loads) for review in run.tables) or 1
    done = 0
    for review in run.tables:
        ids = [load.ingestion_id for load in review.loads]
        source_table = _source_table(review.table_name)
        _progress(run, done / total, f"Cleansing {review.table_name}")
        _ensure_cleansed(conn, review.table_name, columns_catalog)
        cleansed = sql.SQL("{}.{}").format(_ident(config.CLEANSED_SCHEMA), _ident(review.table_name))
        # Idempotent: a load retried after a failure never doubles.
        _delete_cleansed(conn, review.table_name, ids)
        for load in review.loads:
            _delete_load(conn, source_table, load.file_name, load.processing_date)
        frame = _bronze_frame(conn, review.table_name, review.columns, ids)
        silver, report = transform.transform(frame, _mapping(review), columns_catalog, review.pc_id, lotl,
                                             _context(review, processed_at), extras=True)
        quality[review.table_name] = report
        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(cleansed, sql.SQL(", ").join(
            _ident(n) for n in target + transform.EXTRAS + ["_silver_run_id", "_cleansed_at"]))
        with conn.cursor() as cursor, cursor.copy(copy_sql) as copy:
            for row in silver.select(target + transform.EXTRAS).iter_rows():
                copy.write_row(row + (run.id, processed_at))
        expected = Counter(frame["_ingestion_id"].to_list())
        counts = dict(conn.execute(sql.SQL("SELECT _ingestion_id, count(*) FROM {} WHERE _ingestion_id = ANY(%s) "
                                           "GROUP BY _ingestion_id").format(cleansed), [ids]).fetchall())
        if any(counts.get(i, 0) != expected.get(i, 0) for i in ids):
            raise ApiError(500, "row_mismatch", f"{review.table_name}: cleansed row counts do not match bronze.",
                           "Nothing was loaded. Retry, and report it if it happens again.")
        _progress(run, done / total, f"Loading {review.table_name} into {DETAIL}")
        inserted = conn.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} WHERE _ingestion_id = ANY(%s)").format(
            detail, names, names, cleansed), [ids]).rowcount
        if inserted != frame.height:
            raise ApiError(500, "row_mismatch", f"{review.table_name}: Silver row counts do not match bronze.",
                           "Nothing was loaded. Retry, and report it if it happens again.")
        for load in review.loads:
            run.loaded[load.ingestion_id] = counts.get(load.ingestion_id, 0)
        job_history.note(run.id, f"{review.table_name}: {inserted} rows into {config.CLEANSED_SCHEMA}.{review.table_name} "
                                 f"and {config.SILVER_SCHEMA}.{DETAIL}")
        loaded += inserted
        conn.execute(sql.SQL("UPDATE {}.ingestion SET silver_status = 'succeeded', silver_run_id = %s, "
                             "silver_loaded_at = now() WHERE id = ANY(%s)").format(control), [run.id, ids])
        done += len(ids)

    # 4. The aggregate, rebuilt for every source system this run touched.
    sources = sorted({review.source_system for review in run.tables if review.source_system}
                     | {item["source_system"] for item in cleanup if item.get("source_system")})
    _rebuild_aggregate(conn, sources)

    # 5. The audit: what was approved, how it was chosen, and every method's vote.
    conn.execute(sql.SQL(
        "INSERT INTO {}.silver_run (id, ingestion_ids, reviewed_by, approved_by, mapping, quality, rows_loaded, "
        "rows_removed, status, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'succeeded', clock_timestamp())"
    ).format(control),
        [run.id, [l.ingestion_id for r in run.tables for l in r.loads], run.reviewed_by, run.approved_by,
         Jsonb(_mapping_snapshot(run)), Jsonb(_json_ready(quality)), loaded, removed])
    run.result = {"rows_loaded": loaded, "rows_removed": removed, "mappings_saved": saved, "quality": quality}

    # 6. The review is done -- committed together with the rows it loaded.
    run.status, run.progress, run.message, run.finished_at = SUCCEEDED, 1.0, "Loaded", time.time()
    _write_run(conn, run)


def _mapping_snapshot(run: Run) -> list[dict]:
    """The approved mapping with how each column was chosen and every method's vote."""
    return [
        {"pc_id": r.pc_id, "pc_ids": r.pc_ids, "bronze_table_name": r.table_name, "bronze_column_name": s.bronze_column,
         "source_header": r.headers.get(s.bronze_column), "silver_column_name": s.silver_column, "also": s.also,
         "ignored": s.ignored, "selection": s.selection, "methods": [v.method for v in s.backers()], "split": s.split,
         "votes": [{"method": v.method, "silver_column": v.silver_column, "score": v.score,
                    "second_choice": v.second_choice} for c in s.candidates for v in c.votes]}
        for r in run.tables for s in r.suggestions
    ]


# Aggregate grain: one row per source system, profit center and accounting month. Sums of
# the additive measures and the policy count are derived; columns the detail cannot
# supply (dimensions below the profit center, ratios, customer counts) stay NULL.
RECORD_GRAIN = "PROFIT_CENTER_MONTH"
REPORT_TYPE = "POLICY_TRANSACTION_SUMMARY"


def _rebuild_aggregate(conn, sources: list[str]) -> None:
    from psycopg import sql

    if not sources:
        return
    schema = _ident(config.SILVER_SCHEMA)
    conn.execute(sql.SQL("DELETE FROM {}.{} WHERE source_system = ANY(%s)").format(schema, _ident(AGGREGATE)), [sources])
    conn.execute(sql.SQL(
        "INSERT INTO {s}.{agg} (record_grain, report_type, agg_data_source, source_system, source_table, "
        "profit_center_number, profit_center_name, aggregation_category, aggregation_dimension_1_name, "
        "aggregation_dimension_1_value, reporting_start_date, reporting_end_date, reporting_year, reporting_month, "
        "reporting_period, reporting_period_type, accounting_effective_date, premium, policy_fees, "
        "gross_commission_amount, producer_commission_amount, revenue, policy_count, business_key_hash, row_hash, "
        "file_date, ingestion_timestamp, processed_timestamp, silver_insert_timestamp, silver_update_timestamp, "
        "source_data_period_start_date, source_data_period_end_date, source_data_period_type) "
        "SELECT %s, %s, %s, g.source_system, %s, g.profit_center_number, g.profit_center_name, 'PROFIT_CENTER', "
        "'profit_center_number', g.profit_center_number, g.month, "
        "(g.month + interval '1 month' - interval '1 day')::date, extract(year FROM g.month)::int, "
        "extract(month FROM g.month)::int, to_char(g.month, 'YYYY-MM'), "
        "CASE WHEN g.month IS NULL THEN NULL ELSE 'MONTH' END, g.month, g.premium, g.policy_fees, g.gross, "
        "g.producer, g.revenue, g.policies, "
        "encode(sha256(convert_to(concat_ws('|', g.source_system, g.profit_center_number, "
        "to_char(g.month, 'YYYY-MM')), 'UTF8')), 'hex'), "
        "encode(sha256(convert_to(concat_ws('|', g.premium, g.policy_fees, g.gross, g.producer, g.revenue, "
        "g.policies), 'UTF8')), 'hex'), "
        "g.file_date, g.ingestion_timestamp, now(), now(), now(), g.period_start, g.period_end, "
        "CASE WHEN g.period_start IS NULL THEN NULL "
        "WHEN date_trunc('month', g.period_start) = date_trunc('month', g.period_end) THEN 'MONTH' "
        "ELSE 'DATE_RANGE' END "
        "FROM (SELECT d.source_system, d.profit_center_number, max(d.profit_center_name) AS profit_center_name, "
        "date_trunc('month', d.accounting_effective_date)::date AS month, sum(d.premium) AS premium, "
        "sum(d.policy_fees) AS policy_fees, sum(d.gross_commission_amount) AS gross, "
        "sum(d.producer_commission_amount) AS producer, sum(d.revenue) AS revenue, "
        "count(DISTINCT d.policy_number) AS policies, max(i.file_received_date) AS file_date, "
        "max(d.ingestion_timestamp) AS ingestion_timestamp, min(d.source_data_period_start_date) AS period_start, "
        "max(d.source_data_period_end_date) AS period_end "
        "FROM {s}.{detail} d LEFT JOIN {c}.ingestion i ON i.file_name = d.source_file "
        "AND coalesce(i.processing_date, i.created_at) = d.ingestion_timestamp "
        "WHERE d.source_system = ANY(%s) "
        "GROUP BY d.source_system, d.profit_center_number, date_trunc('month', d.accounting_effective_date)::date) g"
    ).format(s=schema, agg=_ident(AGGREGATE), detail=_ident(DETAIL), c=_ident(config.CONTROL_SCHEMA)),
        [RECORD_GRAIN, REPORT_TYPE, DETAIL, f"{config.SILVER_SCHEMA}.{DETAIL}", sources])


def _record_failure(run: Run) -> None:
    """After the load's transaction rolled back: the review, the audit and each load say
    it failed. A failed load stays eligible and can be run again."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    ids = [l.ingestion_id for r in run.tables for l in r.loads]
    control = _ident(config.CONTROL_SCHEMA)
    try:
        with db.connection() as conn:
            _write_run(conn, run)
            conn.execute(sql.SQL(
                "INSERT INTO {}.silver_run (id, ingestion_ids, reviewed_by, approved_by, mapping, status, error, "
                "finished_at) VALUES (%s, %s, %s, %s, %s, 'failed', %s, now()) ON CONFLICT (id) DO UPDATE SET "
                "status = 'failed', error = EXCLUDED.error, mapping = EXCLUDED.mapping, finished_at = now()").format(control),
                [run.id, ids, run.reviewed_by or "", run.approved_by, Jsonb(_mapping_snapshot(run)),
                 json.dumps(run.error)])
            conn.execute(sql.SQL(
                "UPDATE {}.ingestion SET silver_status = 'failed', silver_run_id = %s WHERE id = ANY(%s) "
                "AND status = 'ingested' AND silver_status IS DISTINCT FROM 'succeeded'").format(control), [run.id, ids])
    except Exception:  # noqa: BLE001 - the original error is what the reviewer needs
        log.exception("Silver run %s: the failure could not be recorded", run.id)


# --- browsing --------------------------------------------------------------------


def saved_mapping() -> list[dict]:
    """The DRT column mapping (filled from assets/ when the database is first used)."""
    from psycopg import sql

    with db.connection() as conn:
        if not _table_exists(conn, config.SILVER_SCHEMA, DRT_TABLE):
            return []
        rows = conn.execute(sql.SQL(
            "SELECT profit_center, pc_column, drt_column, silver_column_name FROM {}.{} "
            "ORDER BY profit_center, pc_column, silver_column_name NULLS LAST, drt_column NULLS LAST")
            .format(_ident(config.SILVER_SCHEMA), _ident(DRT_TABLE))).fetchall()
    return [{"profit_center": r[0], "pc_column": r[1], "drt_column": r[2], "silver_column_name": r[3]} for r in rows]


_ONE_ROW = ("profit_center = %s AND pc_column = %s AND drt_column IS NOT DISTINCT FROM %s "
            "AND silver_column_name IS NOT DISTINCT FROM %s")


def edit_saved_mapping(profit_center_code: str, pc_column: str, drt_column: str | None, silver_column: str | None,
                       new_silver_column: str | None) -> dict:
    """Point one DRT mapping row at another Silver column; it applies from the next Silver
    run. The row is found by all four of its values (a source column can have two rows,
    and the unique index makes the four of them name exactly one). Its DRT label becomes
    the business's label for the new Silver column, or none."""
    from psycopg import errors, sql

    columns_catalog = catalog()
    targets = {c.name for c in columns_catalog if c.role == MAPPED}
    if not new_silver_column:
        raise ApiError(422, "silver_column_required", "Choose the Silver column this source column loads into.",
                       "To stop using this row, remove it instead.", field="new_silver_column_name")
    if new_silver_column not in targets:
        raise ApiError(422, "unknown_silver_column", f"'{new_silver_column}' is not a Silver column a bronze column can map to.",
                       field="new_silver_column_name")
    try:
        with db.connection() as conn:
            drt = reference_service.drt_labels(conn, columns_catalog).get(new_silver_column)
            updated = conn.execute(sql.SQL(
                "UPDATE {}.{} SET silver_column_name = %s, drt_column = %s WHERE " + _ONE_ROW).format(
                    _ident(config.SILVER_SCHEMA), _ident(DRT_TABLE)),
                [new_silver_column, drt, profit_center_code, pc_column, drt_column, silver_column]).rowcount
    except errors.UniqueViolation as error:
        raise conflict(f"{pc_column} already maps to {new_silver_column} for {profit_center_code}.") from error
    if not updated:
        raise not_found("That mapping row")
    return {"profit_center": profit_center_code, "pc_column": pc_column, "drt_column": drt,
            "silver_column_name": new_silver_column}


def delete_saved_mapping(profit_center_code: str, pc_column: str, drt_column: str | None,
                         silver_column: str | None) -> None:
    """Remove one DRT mapping row (found by all four of its values)."""
    from psycopg import sql

    with db.connection() as conn:
        removed = conn.execute(sql.SQL("DELETE FROM {}.{} WHERE " + _ONE_ROW).format(
            _ident(config.SILVER_SCHEMA), _ident(DRT_TABLE)),
            [profit_center_code, pc_column, drt_column, silver_column]).rowcount
    if not removed:
        raise not_found("That mapping row")


def _json(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return value.isoformat()
    return value


def aggregate() -> list[dict]:
    """Every silver_aggregate row, every column."""
    from psycopg import sql

    with db.connection() as conn:
        if not _table_exists(conn, config.SILVER_SCHEMA, AGGREGATE):
            return []
        cursor = conn.execute(sql.SQL(
            "SELECT * FROM {}.{} ORDER BY source_system, profit_center_number, reporting_period NULLS LAST")
            .format(_ident(config.SILVER_SCHEMA), _ident(AGGREGATE)))
        names = [column.name for column in cursor.description]
        rows = cursor.fetchall()
    return [{name: _json(value) for name, value in zip(names, row)} for row in rows]
