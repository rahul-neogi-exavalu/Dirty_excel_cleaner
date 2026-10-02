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
   saves the approved mapping into ``drt_column_mapping`` (and only then: suggestions
   are never saved), removes rows of superseded bronze loads, loads the cleansed rows
   into ``silver_detail``, rebuilds ``silver_aggregate`` and marks each load done.

``silver_detail`` and ``silver_aggregate`` have exactly the business's columns (see
``backend/config``). A bronze load's rows are identified in Silver by
(source_table, source_file, ingestion_timestamp = the load's processing_date).
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

import polars as pl

from ahi_bronze import file_meta
from ahi_silver import catalog as catalog_module
from ahi_silver import matching, normalize, profit_center, semantic, transform
from ahi_silver.catalog import MAPPED, SilverColumn
from ahi_silver.matching import Suggestion

from .. import config, db
from ..errors import ApiError, conflict, not_found
from . import job_history, reference_service

DRAFT, RUNNING, SUCCEEDED, FAILED = "draft", "running", "succeeded", "failed"
DETAIL = "silver_detail"
AGGREGATE = "silver_aggregate"
DRT_TABLE = reference_service.DRT_TABLE
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
    pc_id: str | None = None
    # The bronze load time: Silver's ingestion_timestamp and part of the load's identity.
    processing_date: datetime | None = None
    file_date: date | None = None
    division_name: str | None = None


@dataclass
class TableReview:
    table_name: str
    source_system: str | None
    # The loads' profit center (PC0796): the key of the DRT column mapping.
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
            "i.rows_loaded, i.created_at, i.silver_status, i.pc_id, i.division_name, i.file_date, "
            "coalesce(i.processing_date, i.created_at) FROM {}.ingestion i "
            "WHERE i.status = 'ingested' AND i.silver_status IS DISTINCT FROM 'succeeded' "
            "ORDER BY i.created_at DESC").format(_ident(config.CONTROL_SCHEMA))).fetchall()
        cleanup = _superseded(conn)
    return {
        "loads": [
            {"ingestion_id": r[0], "table_name": r[1], "file_name": r[2], "source_system": r[3],
             "period_start": r[4], "period_end": r[5], "rows": r[6], "ingested_at": r[7].timestamp(),
             "silver_status": r[8], "pc_id": r[9] or file_meta.normalize_pc_id(r[3]), "division_name": r[10],
             "file_date": r[11].isoformat() if r[11] else None, "processing_date": r[12].timestamp()}
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
    """Every DRT column mapping row: (profit_center, pc_column, drt_column, silver_column_name)."""
    from psycopg import sql

    if not _table_exists(conn, config.SILVER_SCHEMA, DRT_TABLE):
        return []
    return conn.execute(sql.SQL(
        "SELECT profit_center, pc_column, drt_column, silver_column_name FROM {}.{}")
        .format(_ident(config.SILVER_SCHEMA), _ident(DRT_TABLE))).fetchall()


def _same_pc(left: str | None, right: str | None) -> bool:
    return bool(left and right) and file_meta.normalize_pc_id(left) == file_meta.normalize_pc_id(right)


def _saved(rows: list[tuple], pc: str | None, columns: list[str]) -> tuple[dict, dict[str, str], set[str]]:
    """This profit center's approved mapping for these bronze columns, and approved names
    elsewhere (by normalized name).

    The DRT mapping names a source column as the file wrote it ("Net Premium"); a bronze
    column is its cleaned form ("net_premium"). They meet on their normalized words.
    Returns ({column: [silver, ...] or None (ignored)}, {normalized name: silver}, and the
    columns whose several targets are one header mapped twice -- a real one-to-many).
    Two different headers that normalize alike ("Agent Commission" and "Agent
    Commission%") are not one-to-many: their targets compete as votes.
    """
    by_compact = {normalize.compact(column): column for column in columns}
    own: dict[str, list[str] | None] = {}
    headers: dict[str, set[str]] = {}
    ignored: set[str] = set()
    votes: dict[str, Counter] = {}
    for profit_center_code, pc_column, drt_column, silver in rows:
        key = normalize.compact(pc_column or "")
        if not key:
            continue
        if _same_pc(profit_center_code, pc):
            column = by_compact.get(key)
            if column is None:
                continue
            if silver:
                targets = own.get(column) or []
                if silver not in targets:
                    own[column] = targets + [silver]
                headers.setdefault(column, set()).add(" ".join(pc_column.split()).casefold())
            elif drt_column is None:
                ignored.add(column)
        elif silver:
            votes.setdefault(key, Counter())[silver] += 1
    for column in ignored:
        own.setdefault(column, None)
    known = {name: counter.most_common(1)[0][0] for name, counter in votes.items()}
    one_to_many = {column for column, targets in own.items() if targets and len(targets) > 1
                   and len(headers.get(column, ())) == 1}
    return own, known, one_to_many


# Notes the business keeps in the mapping's source-column field ("check comments",
# "08/05: Sara suggested ...") are not column names: never shown to the AI as examples.
_NOT_A_COLUMN = ("comment", "disregard", "sara", "check ", "suggested", "backfill", "(tab", "data profile")


def _precedents(rows: list[tuple], pc: str | None = None) -> list[tuple[str, str | None]]:
    """Approved decisions worth showing the AI as examples, this profit center's first:
    a renamed or abbreviated column (Bk MGA Comm -> gross_commission_amount) or a
    deliberate ignore. A column named exactly like its Silver column teaches nothing."""
    picked: dict[tuple[str, str | None], bool] = {}
    for profit_center_code, pc_column, drt_column, silver in rows:
        header = " ".join((pc_column or "").split())
        if not header or len(header) > 40 or any(word in header.casefold() for word in _NOT_A_COLUMN):
            continue
        if silver is None and drt_column is not None:
            continue  # a DRT label without a Silver column: not a decision
        if silver is not None and normalize.compact(header) == normalize.compact(silver):
            continue
        pair = (header, silver)
        picked[pair] = picked.get(pair, False) or _same_pc(profit_center_code, pc)
    return sorted(picked, key=lambda pair: (not picked[pair], pair[0].casefold(), pair[1] or ""))


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
    # Read everything first, then let go of the connection: matching may call the AI or
    # load word2vec vectors, and neither should hold one of the pool's few connections.
    with db.connection() as conn:
        rows = conn.execute(sql.SQL(
            "SELECT id, table_name, file_name, source_system, period_start, period_end, rows_loaded, status, "
            "silver_status, job_id, file_sha256, pc_id, coalesce(processing_date, created_at), file_date, "
            "division_name FROM {}.ingestion WHERE id = ANY(%s)").format(_ident(config.CONTROL_SCHEMA)), [ids]).fetchall()
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
        # The DRT column mapping: created and filled from assets/ the first time.
        reference_service.ensure_drt_table(conn, columns_catalog)
        approved = _mapping_rows(conn)
        for table, members in groups.items():
            source = members[0][3]
            pc, seen = _table_pc(members, source)
            if len(seen) > 1:
                notes.append(f"{table}: loads from {', '.join(seen)}; the mapping uses {pc}.")
            columns = _bronze_columns(conn, table)
            frame = _bronze_frame(conn, table, columns, [m[0] for m in members])
            own, known, one_to_many = _saved(approved, pc, columns)
            gathered.append((table, members, source, pc, columns, frame, own, known, one_to_many))
        lotl = _lotl(conn)
        cleanup = _superseded(conn)
    if not ids and not cleanup:
        raise conflict("There is nothing to load or remove.", "Select at least one bronze load.")

    first_pc = gathered[0][3] if gathered else None
    similarity, llm, matcher_notes = _matchers(_precedents(approved, first_pc))
    notes.extend(matcher_notes)
    tables = []
    for table, members, source, pc, columns, frame, own, known, one_to_many in gathered:
        suggestions, step_notes = matching.suggest(
            columns, columns_catalog, own, known, _samples(frame.head(SAMPLE_ROWS), columns),
            similarity=similarity, llm=llm, fuzzy_min=config.MATCH_FUZZY_MIN,
            semantic_min=config.MATCH_SEMANTIC_MIN, one_to_many=one_to_many,
        )
        notes.extend(note for note in step_notes if note not in notes)
        review = TableReview(
            table_name=table, source_system=source, pc_id=pc,
            loads=[Load(m[0], m[2], m[6], m[4], m[5], job_id=m[9], file_sha256=m[10], pc_id=m[11],
                        processing_date=m[12], file_date=m[13], division_name=m[14]) for m in members],
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


def _mapping(review: TableReview) -> list[tuple[str, str]]:
    """(bronze column, Silver column) pairs; one bronze column may feed several."""
    return [(s.bronze_column, target) for s in review.suggestions for target in s.targets]


def _context(review: TableReview, processed_at: datetime | None = None) -> transform.Context:
    return transform.Context(
        source_system=review.source_system,
        source_table=_source_table(review.table_name),
        processed_at=processed_at,
        loads={load.ingestion_id: transform.LoadInfo(load.file_name, load.processing_date, load.period_start,
                                                      load.period_end) for load in review.loads},
    )


def _quality(review: TableReview, columns_catalog, lotl) -> None:
    """The dry run the reviewer sees: the load's own transform, on the rows read at review time."""
    _, quality = transform.transform(review.frame, _mapping(review), columns_catalog, review.pc_id, lotl,
                                     _context(review))
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


def update_mapping(run_id: str, table: str, column: str, silver_column: str | None, ignored: bool,
                   fields: set[str] | None = None, also: list[str] | None = None) -> Run:
    """The reviewer's choice for one column. ``fields``: which of silver_column / ignored /
    also were sent (all of the first two when not given). ``also``: the extra Silver
    columns the bronze column loads into, as a whole list."""
    run = get_run(run_id)
    columns_catalog = catalog()
    targets = {c.name for c in columns_catalog if c.role == MAPPED}
    fields = {"silver_column", "ignored"} if fields is None else fields
    for name in [silver_column, *(also or [])]:
        if name is not None and name not in targets:
            raise ApiError(422, "unknown_silver_column", f"'{name}' is not a Silver column a bronze column can map to.",
                           field="silver_column")
    with run.lock:
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
    target = f"{config.SILVER_SCHEMA}.{DETAIL}"
    return [
        {"output_name": review.table_name, "output_file": target, "row_count": run.loaded.get(load.ingestion_id),
         "source_file": load.file_name, "source_sha256": load.file_sha256, "source_job_id": load.job_id}
        for review in run.tables for load in review.loads
    ]


def _column_sql(column: SilverColumn, identity: str):
    from psycopg import sql

    generated = " GENERATED BY DEFAULT AS IDENTITY" if column.name == identity else ""
    return sql.SQL("{} {}").format(_ident(column.name), sql.SQL(column.sql_type + generated))


def _ensure_table(conn, table: str, columns: list[SilverColumn], identity: str) -> None:
    """Create a Silver table with exactly these columns, or add the ones it lacks."""
    from psycopg import sql

    schema = _ident(config.SILVER_SCHEMA)
    conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
        schema, _ident(table), sql.SQL(", ").join(_column_sql(c, identity) for c in columns)))
    present = {row[0] for row in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
        [config.SILVER_SCHEMA, table])}
    for column in columns:
        if column.name not in present:
            conn.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN {}").format(schema, _ident(table), _column_sql(column, "")))


def _ensure_tables(conn, columns_catalog: list[SilverColumn], aggregate_columns: list[SilverColumn]) -> None:
    from psycopg import sql

    schema = _ident(config.SILVER_SCHEMA)
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
    reference_service.ensure_drt_table(conn, columns_catalog)
    _ensure_table(conn, DETAIL, columns_catalog, transform.IDENTITY)
    # A bronze load's rows: what replacing or reloading it deletes.
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS silver_detail_load ON {}.{} "
                         "(source_table, source_file, ingestion_timestamp)").format(schema, _ident(DETAIL)))
    _ensure_table(conn, AGGREGATE, aggregate_columns, "ahi_aggregate_id")
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS silver_aggregate_source ON {}.{} (source_system)")
                 .format(schema, _ident(AGGREGATE)))


def _delete_load(conn, source_table: str, file_name: str, processing_date) -> int:
    from psycopg import sql

    return conn.execute(sql.SQL(
        "DELETE FROM {}.{} WHERE source_table = %s AND source_file = %s AND ingestion_timestamp = %s").format(
            _ident(config.SILVER_SCHEMA), _ident(DETAIL)), [source_table, file_name, processing_date]).rowcount


def _save_mapping(conn, run: Run, columns_catalog: list[SilverColumn]) -> int:
    """Save every approved decision into the DRT column mapping, per profit center.

    A bronze column's rows (matched on normalized words, so "Net Premium" and
    "net_premium" are one column) become exactly its approved targets: rows for other
    Silver columns go, missing ones are added under the header as the mapping already
    writes it. An ignored column is one row with no DRT and no Silver column. Rows that
    name a DRT column the catalog does not resolve are left alone.
    """
    from psycopg import sql

    table = sql.SQL("{}.{}").format(_ident(config.SILVER_SCHEMA), _ident(DRT_TABLE))
    drt = {c.name: c.drt_name or None for c in columns_catalog}
    rows = _mapping_rows(conn)
    saved = 0
    for review in run.tables:
        if not review.pc_id:
            continue
        mine = [row for row in rows if _same_pc(row[0], review.pc_id)]
        for s in review.suggestions:
            key = normalize.compact(s.bronze_column)
            current = [row for row in mine if normalize.compact(row[1] or "") == key and (row[3] or row[2] is None)]
            wanted: list[str | None] = [None] if s.ignored else list(s.targets)
            if not wanted:
                continue
            for profit_center_code, pc_column, drt_column, silver in current:
                if silver not in wanted:
                    conn.execute(sql.SQL(
                        "DELETE FROM {} WHERE profit_center = %s AND pc_column = %s AND "
                        "silver_column_name IS NOT DISTINCT FROM %s AND drt_column IS NOT DISTINCT FROM %s").format(table),
                        [profit_center_code, pc_column, silver, drt_column])
            have = {row[3] for row in current}
            header = current[0][1] if current else s.bronze_column
            code = current[0][0] if current else review.pc_id
            for target in wanted:
                if target in have:
                    continue
                conn.execute(sql.SQL(
                    "INSERT INTO {} (profit_center, pc_column, drt_column, silver_column_name) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING").format(table),
                    [code, header, drt.get(target) if target else None, target])
                saved += 1
    return saved


def _execute(conn, run: Run) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    columns_catalog = catalog()
    aggregate_columns = aggregate_catalog()
    schema = _ident(config.SILVER_SCHEMA)
    control = _ident(config.CONTROL_SCHEMA)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('ahi-silver'))")
    # The authoritative check: under the lock no other Silver run can interleave.
    _still_valid(conn, run, columns_catalog)
    _ensure_tables(conn, columns_catalog, aggregate_columns)

    # 1. The approved mapping -- every row, including the pre-filled ones -- is saved.
    _save_mapping(conn, run, columns_catalog)

    # 2. Rows of bronze loads that were replaced since they reached Silver.
    cleanup = _superseded(conn)
    removed = 0
    if cleanup:
        for item in cleanup:
            removed += _delete_load(conn, _source_table(item["table_name"]), item["file_name"],
                                    datetime.fromisoformat(item["processing_date"]))
        conn.execute(sql.SQL("UPDATE {}.ingestion SET silver_status = 'removed' WHERE id = ANY(%s)").format(control),
                     [[item["ingestion_id"] for item in cleanup]])

    # 3. Load each table's selected loads.
    lotl = _lotl(conn)
    processed_at = datetime.now(timezone.utc)
    target = [c.name for c in columns_catalog if c.name != transform.IDENTITY]
    copy_sql = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        schema, _ident(DETAIL), sql.SQL(", ").join(_ident(n) for n in target))
    loaded, quality = 0, {}
    total = sum(len(review.loads) for review in run.tables) or 1
    done = 0
    for review in run.tables:
        ids = [load.ingestion_id for load in review.loads]
        source_table = _source_table(review.table_name)
        # Idempotent: a load retried after a failure never doubles.
        for load in review.loads:
            _delete_load(conn, source_table, load.file_name, load.processing_date)
        frame = _bronze_frame(conn, review.table_name, review.columns, ids)
        silver, report = transform.transform(frame, _mapping(review), columns_catalog, review.pc_id, lotl,
                                             _context(review, processed_at))
        quality[review.table_name] = report
        run.message = f"Loading {review.table_name}"
        with conn.cursor() as cursor, cursor.copy(copy_sql) as copy:
            for row in silver.select(target).iter_rows():
                copy.write_row(row)
        expected = Counter(frame["_ingestion_id"].to_list())
        for load in review.loads:
            count = conn.execute(sql.SQL(
                "SELECT count(*) FROM {}.{} WHERE source_table = %s AND source_file = %s AND ingestion_timestamp = %s")
                .format(schema, _ident(DETAIL)), [source_table, load.file_name, load.processing_date]).fetchone()[0]
            if count != expected.get(load.ingestion_id, 0):
                raise ApiError(500, "row_mismatch", f"{review.table_name}: Silver row counts do not match bronze.",
                               "Nothing was loaded. Retry, and report it if it happens again.")
            run.loaded[load.ingestion_id] = count
        job_history.note(run.id, f"{review.table_name}: {silver.height} rows into {config.SILVER_SCHEMA}.{DETAIL}")
        loaded += silver.height
        conn.execute(sql.SQL("UPDATE {}.ingestion SET silver_status = 'succeeded', silver_run_id = %s, "
                             "silver_loaded_at = now() WHERE id = ANY(%s)").format(control), [run.id, ids])
        done += len(ids)
        run.progress = done / total

    # 4. The aggregate, rebuilt for every source system this run touched.
    sources = sorted({review.source_system for review in run.tables if review.source_system}
                     | {item["source_system"] for item in cleanup if item.get("source_system")})
    _rebuild_aggregate(conn, sources)

    # The audit snapshot: what was approved, how it was chosen, and every method's vote.
    mapping = [
        {"pc_id": r.pc_id, "bronze_table_name": r.table_name, "bronze_column_name": s.bronze_column,
         "silver_column_name": s.silver_column, "also": s.also, "ignored": s.ignored, "selection": s.selection,
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
        "count(DISTINCT d.policy_number) AS policies, max(i.file_date) AS file_date, "
        "max(d.ingestion_timestamp) AS ingestion_timestamp, min(d.source_data_period_start_date) AS period_start, "
        "max(d.source_data_period_end_date) AS period_end "
        "FROM {s}.{detail} d LEFT JOIN {c}.ingestion i ON i.file_name = d.source_file "
        "AND coalesce(i.processing_date, i.created_at) = d.ingestion_timestamp "
        "WHERE d.source_system = ANY(%s) "
        "GROUP BY d.source_system, d.profit_center_number, date_trunc('month', d.accounting_effective_date)::date) g"
    ).format(s=schema, agg=_ident(AGGREGATE), detail=_ident(DETAIL), c=_ident(config.CONTROL_SCHEMA)),
        [RECORD_GRAIN, REPORT_TYPE, DETAIL, f"{config.SILVER_SCHEMA}.{DETAIL}", sources])


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
    """The DRT column mapping (created and filled from assets/ on first use)."""
    from psycopg import sql

    with db.connection() as conn:
        reference_service.ensure_drt_table(conn, catalog())
        rows = conn.execute(sql.SQL(
            "SELECT profit_center, pc_column, drt_column, silver_column_name FROM {}.{} "
            "ORDER BY profit_center, pc_column, silver_column_name NULLS FIRST")
            .format(_ident(config.SILVER_SCHEMA), _ident(DRT_TABLE))).fetchall()
    return [{"profit_center": r[0], "pc_column": r[1], "drt_column": r[2], "silver_column_name": r[3]} for r in rows]


def edit_saved_mapping(profit_center_code: str, pc_column: str, silver_column: str | None,
                       new_silver_column: str | None) -> dict:
    """Point one DRT mapping row at another Silver column (or none = ignore); it applies
    from the next Silver run. The row is the one for (profit center, source column,
    current Silver column), since one source column can have two rows."""
    from psycopg import errors, sql

    columns_catalog = {c.name: c for c in catalog() if c.role == MAPPED}
    if new_silver_column is not None and new_silver_column not in columns_catalog:
        raise ApiError(422, "unknown_silver_column", f"'{new_silver_column}' is not a Silver column a bronze column can map to.",
                       field="silver_column_name")
    drt = (columns_catalog[new_silver_column].drt_name or None) if new_silver_column else None
    # Ignoring clears the DRT label too; a new target takes its DRT label (or keeps the row's).
    assign = ("silver_column_name = %s, drt_column = NULL" if new_silver_column is None
              else "silver_column_name = %s, drt_column = coalesce(%s::text, drt_column)")
    values = [new_silver_column] if new_silver_column is None else [new_silver_column, drt]
    try:
        with db.connection() as conn:
            reference_service.ensure_drt_table(conn, catalog())
            updated = conn.execute(sql.SQL(
                "UPDATE {}.{} SET " + assign + " WHERE profit_center = %s AND pc_column = %s "
                "AND silver_column_name IS NOT DISTINCT FROM %s").format(_ident(config.SILVER_SCHEMA), _ident(DRT_TABLE)),
                values + [profit_center_code, pc_column, silver_column]).rowcount
    except errors.UniqueViolation as error:
        raise conflict(f"{pc_column} already maps to {new_silver_column} for {profit_center_code}.") from error
    if not updated:
        raise not_found("That mapping row")
    return {"profit_center": profit_center_code, "pc_column": pc_column, "drt_column": drt,
            "silver_column_name": new_silver_column}


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
