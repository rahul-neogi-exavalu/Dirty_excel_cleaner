"""Validate: is each cleaned file fit for Bronze? Then stage it and record it in the control table.

After cleaning, every output of every file is checked against the business's rules
(``ahi_bronze.validation``):

1. **Required columns.** Its columns are matched to the required ones the way the Silver
   review matches them (``ahi_silver.matching``): the bronze column mapping votes first
   (seeded from the Silver DRT mapping, grown by every file staged here), then exact,
   fuzzy, word2vec and AI votes. Every required column must be found, or one of each
   group of alternatives (``one_of``).
2. **Reporting dates.** AED, PED, TED: the first populated on every row decides them, in
   whole months; otherwise the reviewer enters them. Year-to-date or monthly; anything
   else is flagged, and a flagged file is rejected unless the reviewer corrects the dates.
3. **Against Bronze.** What the control table says this profit center already has in
   Bronze decides INSERT, APPEND or a choice for the reviewer (a month already loaded).
4. **The whole file.** A file is fit only when every one of its sheets is: one rejected
   rejects them all, and one waiting for the reviewer holds the others back.

The reviewer fixes what needs a person, then stages -- a file whole, every sheet at once.
Staging writes, in one transaction: the new mappings into the bronze column mapping, each
output's rows into a staging table, and its control-table row (file and sheet) -- INSERT,
APPEND or REJECTED -- which drives the Ingest step. A file the control table already lists
(seeded from the business's workbook, or staged before and not loaded) fills in that row
instead of adding one.

Building a session takes a while (the database, word2vec, the AI), so it runs on a worker
thread: ``start`` answers at once, and the session's tracker reports each step as it
finishes -- streamed to the browser as Server-Sent Events (``api.progress``). It can be
edited and staged once built.

Sessions live in memory, like cleaning jobs and Bronze plans; what is staged is in the
database.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache

import polars as pl
from fastapi.encoders import jsonable_encoder

from ahi_bronze import commission, file_meta, grain, naming
from ahi_bronze import validation as rules
from ahi_bronze.schema_compare import compare as compare_schema
from ahi_bronze.schema_compare import recommend as recommend_schema
from ahi_bronze.validation import Decision, Loaded
from ahi_clean.orchestrate import SOURCE_SHEET_COLUMN
from ahi_silver import matching, semantic

from .. import config, db, progress
from ..errors import ApiError, conflict, not_found
from ..store import SUCCEEDED, OutputRecord, store
from . import bronze_service, column_matching, reference_service

READY, NEEDS_INPUT, FLAGGED, REJECTED = "ready", "needs_input", "flagged", "rejected"
SAMPLE_ROWS = 200
CHOICES = (rules.REJECT, rules.REPLACE, rules.REPLACE_MONTH, rules.REVISE, rules.COMPANION)
# Even a rejected file needs it: the control table records its source system.
PC_NEEDED = "Enter the profit center."
SCOPE_NEEDED = "Choose the scope."
DATE_BASIS_NEEDED = "Choose the date basis (AED, TED or PED)."
FILE_TYPE_NEEDED = "Choose incremental or cumulative."
PERIOD_NEEDED = "Enter the data collection period."

log = logging.getLogger("ahi.validation")


@lru_cache(maxsize=1)
def required() -> tuple[rules.Required, ...]:
    """Every column the business ever requires, with its label, date role and one_of group.
    What applies to one file is a scope's subset of these (``required_for``)."""
    return tuple(rules.load_required(config.BRONZE_REQUIRED_COLUMNS_FILE))


# Required columns the reviewer may not fill: the dates (AED / PED / TED), which decide the
# reporting period, and the columns that identify and value a transaction, which come from
# the file. The commission amounts come from the file or from their rate x premium, never
# typed. Every other one may instead be filled -- left null, or one value on every row.
NOT_FILLABLE = frozenset({"premium", "policy_number"}) | commission.AMOUNTS
FILL_NULL, FILL_VALUE = "null", "value"


def fillable(item: rules.Required) -> bool:
    return not item.date_role and item.name not in NOT_FILLABLE


def required_for(session: Session, scope: str | None) -> tuple[rules.Required, ...]:
    """The required columns for a scope (AJG / APD / BOTH): (), until the reviewer chooses
    one, so the required-columns section has nothing to match until then."""
    return session.scope_required.get(scope, ()) if scope else ()


def date_role_items() -> tuple[rules.Required, ...]:
    """The date-role columns (AED / PED / TED): always matching targets, whatever the
    scope's own required columns are -- the date basis is global, not scope-specific, so
    a scope (APD) that does not itself require one of these must still get the chance to
    find it in the file."""
    return tuple(item for item in required() if item.date_role)


def matching_targets(session: Session, scope: str | None) -> tuple[rules.Required, ...]:
    """The scope's required columns, plus whichever date-role columns it does not already
    require: everything column matching should attempt for this file."""
    scoped = required_for(session, scope)
    names = {item.name for item in scoped}
    return scoped + tuple(item for item in date_role_items() if item.name not in names)


def source_system_for(pc_id: str | None) -> str | None:
    """The control table's source system: EXT_ and the profit center (EXT_PC0796)."""
    return f"EXT_{pc_id}" if pc_id else None


@dataclass
class FileCheck:
    job_id: str
    file_name: str
    file_sha256: str
    pc_id: str | None
    detected_pc_id: str | None
    received: date | None
    detected_received: date | None
    received_note: str | None = None
    division_matches: list[str] = field(default_factory=list)
    division_name: str | None = None
    # AJG / APD / BOTH: which required columns apply. Detected from this profit center's
    # control-table rows; the reviewer picks it when none is found.
    scope: str | None = None
    detected_scope: str | None = None
    # AED / TED / PED: the date column rows are filtered by. INCREMENTAL / CUMULATIVE, and
    # the year and month this file reports: together they compute the reporting dates.
    # The first two start as this profit center's last staged choice (the detected_*
    # fields), for the reviewer to confirm; the period is always the reviewer's own.
    date_basis: str | None = None
    detected_date_basis: str | None = None
    reporting_file_type: str | None = None
    detected_reporting_file_type: str | None = None
    data_collection_period: date | None = None  # always the 1st of the month
    warnings: list[str] = field(default_factory=list)

    @property
    def source_system(self) -> str | None:
        return source_system_for(self.pc_id)


@dataclass
class OutputCheck:
    key: str
    job_id: str
    output_id: str
    name: str
    sheet_names: list[str]
    rows: int
    # Every column of the output: original name, bronze name, header as written, and
    # whether it was left out of ingestion on Results.
    columns: list[dict]
    # Required column (Silver name) -> the output's column (original name), or None.
    mapping: dict[str, str | None]
    # Required column -> how its column was found ({methods, method, score, reason}).
    votes: dict[str, dict]
    # Required column -> the columns any method proposed for it, best first.
    options: dict[str, list[dict]]
    reviewer: set[str] = field(default_factory=set)
    # Required column -> how the reviewer filled it instead of mapping a file column:
    # {"kind": "null"} (staged without it) or {"kind": "value", "value": ...} (on every row).
    fills: dict[str, dict] = field(default_factory=dict)
    # Reporting dates the reviewer entered, or the control table's when adopted.
    entered: tuple[date, date] | None = None
    use_control_dates: bool = False
    # The date column the reviewer picked to decide the reporting dates (AED / TED / PED).
    date_role: str | None = None
    # The loaded file a revision replaces, as the reviewer named it.
    replaces_file: str | None = None
    # The file a companion came with: another output's key, or "control:<id>" for one loaded.
    companion_of: str | None = None
    # The reviewer's word on what the table holds: "aggregate" or "transaction" (None: detected).
    grain: str | None = None
    choice: str | None = None
    # An incremental file's earlier months (YYYY-MM) the reviewer opts to also truncate and
    # reload in Bronze, besides its declared data collection month.
    replace_months: list[str] = field(default_factory=list)
    staged: dict | None = None
    result: dict = field(default_factory=dict, repr=False)
    decision: Decision | None = field(default=None, repr=False)


@dataclass
class Session:
    id: str
    batch_id: str | None
    files: list[FileCheck]
    outputs: list[OutputCheck]
    notes: list[str] = field(default_factory=list)
    divisions: list[tuple] = field(default_factory=list, repr=False)
    # Control rows of these files not loaded yet (seeded, or staged before).
    pending: list[dict] = field(default_factory=list, repr=False)
    # source_system -> its files in Bronze (control rows loaded and active)
    loaded: dict[str, list[Loaded]] = field(default_factory=dict, repr=False)
    # scope -> its required columns; profit_center -> its detected scope.
    scope_required: dict[str, tuple[rules.Required, ...]] = field(default_factory=dict, repr=False)
    scope_by_pc: dict[str, str] = field(default_factory=dict, repr=False)
    # profit_center -> its last staged date_basis / reporting_file_type.
    period_choices_by_pc: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    # pc_id -> month (YYYY-MM) -> rows currently in Bronze: for a cumulative file's
    # mismatch check, against the month it is about to replace.
    bronze_counts: dict[str, dict[str, int]] = field(default_factory=dict, repr=False)
    # The bronze column mapping's rows, kept for rematching a file whose scope changes
    # after the session was built (a pc_id entered or corrected later).
    mapping_rows: list[tuple] = field(default_factory=list, repr=False)
    created_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # How far building the session has got; finished once it can be reviewed.
    tracker: progress.Tracker = field(default_factory=lambda: progress.Tracker.of(*PHASES), repr=False)

    def file(self, job_id: str) -> FileCheck:
        found = next((item for item in self.files if item.job_id == job_id), None)
        if found is None:
            raise not_found("That file in the validation")
        return found

    def output(self, key: str) -> OutputCheck:
        found = next((item for item in self.outputs if item.key == key), None)
        if found is None:
            raise not_found("That table in the validation")
        return found


_sessions: dict[str, Session] = {}
_sessions_lock = threading.Lock()


# --- reading what the database knows -------------------------------------------------


@dataclass
class Context:
    divisions: list[tuple] = field(default_factory=list)
    # (profit_center, file_columns, None, silver_column_name): the shape the Silver helpers read.
    mapping_rows: list[tuple] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    loaded: dict[str, list[Loaded]] = field(default_factory=dict)
    scope_required: dict[str, tuple[rules.Required, ...]] = field(default_factory=dict)
    scope_by_pc: dict[str, str] = field(default_factory=dict)
    period_choices_by_pc: dict[str, dict[str, str]] = field(default_factory=dict)
    bronze_counts: dict[str, dict[str, int]] = field(default_factory=dict)


_CONTROL_FIELDS = ("control_id", "source_system", "file_name", "sheet_name", "reporting_period_type",
                   "processing_action", "bronze_load_flag", "file_received_date", "drt_reporting_start_date",
                   "drt_reporting_end_date",
                   "date_detail", "file_replaced", "is_active", "pc_id", "division_name", "job_id", "output_id",
                   "staging_table", "ingestion_id", "rejection_reason", "validation", "replace_month",
                   "created_by", "created_at", "staged_at", "loaded_at", "bronze_table", "companion_of",
                   "is_aggregated", "scope", "date_basis", "reporting_file_type", "data_collection_period")


READ_STEPS = 8


def _read(file_names: list[str], conn=None, tracker: progress.Tracker | None = None) -> Context:
    """The division table, the bronze column mapping, each scope's required columns and each
    profit center's scope, these files' pending control rows, and every profit center's
    files in Bronze (on ``conn`` when given). ``tracker`` is told of connecting (the CONNECT
    phase, when there is no ``conn``) and of each of the reads (READ)."""
    if conn is None:
        if tracker is not None:
            tracker.start(CONNECT, 1, "Opening a connection" if db.prepared() else
                          "Preparing the database on first use: migrations and reference data")
        with db.connection() as own:
            if tracker is not None:
                tracker.advance(1)
            return _read(file_names, own, tracker)
    from psycopg import sql

    def done(detail: str | None = None) -> None:
        if tracker is not None:
            tracker.advance(1, detail)

    if tracker is not None:
        tracker.start(READ, READ_STEPS, "Reading the division table")
    control = sql.SQL("{}.{}").format(sql.Identifier(config.CONTROL_SCHEMA), sql.Identifier("control_table"))
    fields = sql.SQL(", ").join(sql.Identifier(name) for name in _CONTROL_FIELDS)
    context = Context(divisions=reference_service.divisions(conn))
    done("Reading the bronze column mapping")
    context.mapping_rows = [
        (row[0], row[1], None, row[2]) for row in conn.execute(sql.SQL(
            "SELECT profit_center, file_columns, silver_column_name FROM {}.{} ORDER BY profit_center, file_columns")
            .format(sql.Identifier(config.BRONZE_SCHEMA), sql.Identifier(reference_service.MAPPING_TABLE)))
    ]
    done("Reading the Silver DRT column mapping")
    if conn.execute("SELECT to_regclass(%s)", [f'"{config.SILVER_SCHEMA}"."drt_column_mapping"']).fetchone()[0] is not None:
        # Global (the same for every profit center): a weaker, cross-table vote here too,
        # the same way it already stands in for a column approved for another table in
        # Silver -- so a header variation the business approved there (Pol_Ef_Dt's many
        # spellings) also helps Bronze find the column, not just an exact saved mapping.
        context.mapping_rows += [
            (None, row[0], row[1], row[2]) for row in conn.execute(sql.SQL(
                "SELECT pc_column, drt_column, silver_column_name FROM {}.{} WHERE silver_column_name IS NOT NULL")
                .format(sql.Identifier(config.SILVER_SCHEMA), sql.Identifier("drt_column_mapping")))
        ]
    done("Reading the required columns by scope")
    catalog = {item.name: item for item in required()}
    context.scope_required = {
        scope: tuple(catalog[name] for name in [item.name for item in required()] if name in names)
        for scope, names in reference_service.scope_column_names(conn).items()
    }
    done("Reading each profit center's scope")
    context.scope_by_pc = reference_service.scope_by_pc(conn)
    context.period_choices_by_pc = reference_service.period_choices_by_pc(conn)
    done(f"Reading the control rows of {len(file_names)} file{'s' if len(file_names) != 1 else ''}")
    context.pending = [dict(zip(_CONTROL_FIELDS, row)) for row in conn.execute(sql.SQL(
        "SELECT {} FROM {} WHERE file_name = ANY(%s) AND bronze_load_flag = 'N' "
        "AND processing_action IS DISTINCT FROM 'REJECTED' ORDER BY control_id").format(fields, control), [file_names])]
    done("Reading the files already loaded into Bronze")
    for row in conn.execute(sql.SQL(
            "SELECT {} FROM {} WHERE bronze_load_flag = 'Y' AND is_active = 'Y' "
            "AND processing_action IN ('INSERT', 'APPEND') AND drt_reporting_start_date IS NOT NULL "
            "AND drt_reporting_end_date IS NOT NULL ORDER BY control_id").format(fields, control)):
        data = dict(zip(_CONTROL_FIELDS, row))
        context.loaded.setdefault(data["source_system"], []).append(loaded_from(data))
    done("Reading how many rows Bronze currently holds, by profit center and month")
    tables = {(item.pc_id, item.bronze_table) for items in context.loaded.values() for item in items
              if item.pc_id and item.bronze_table and not item.aggregated}
    for pc_id, table in tables:
        if conn.execute("SELECT to_regclass(%s)", [f'"{config.BRONZE_SCHEMA}"."{table}"']).fetchone()[0] is None:
            continue
        for month, count in conn.execute(sql.SQL(
                "SELECT _reporting_month, count(*) FROM {}.{} WHERE pc_id = %s AND _reporting_month IS NOT NULL "
                "GROUP BY _reporting_month").format(sql.Identifier(config.BRONZE_SCHEMA), sql.Identifier(table)),
                [pc_id]):
            counts = context.bronze_counts.setdefault(pc_id, {})
            counts[month] = counts.get(month, 0) + count
    done()
    return context


def loaded_from(row: dict) -> Loaded:
    details = row.get("validation") or {}
    return Loaded(
        control_id=row["control_id"], file_name=row["file_name"], period_type=row["reporting_period_type"],
        start=row["drt_reporting_start_date"], end=row["drt_reporting_end_date"],
        rows=details.get("rows"), columns=len(details.get("columns") or []) or None,
        months=details.get("months") or {},
        received=row.get("file_received_date"), date_detail=row.get("date_detail"),
        present=frozenset(name for name, column in (details.get("mapping") or {}).items() if column),
        column_names=tuple(column["name"] for column in details.get("columns") or []),
        aggregated=row.get("is_aggregated") == "Y",
        bronze_table=row.get("bronze_table"), pc_id=row.get("pc_id"),
    )


# --- building a session --------------------------------------------------------------

# Building a session, in order, with each step's share of the progress bar. Matching is
# most of the work: every method reads every column, and the AI is a network call.
CONNECT, READ, MATCHERS, MATCH, CHECK = "connect", "read", "matchers", "match", "check"
PHASES = (
    (CONNECT, "Connect to the database", 4),
    (READ, "Read the control table and the column mapping", 6),
    (MATCHERS, "Prepare the matchers", 5),
    (MATCH, "Match each table's columns to the required ones", 65),
    (CHECK, "Check reporting dates and the rules", 20),
)
# What matching one column costs each method, relative to the others. Saved and exact
# are lookups; fuzzy and word2vec compare against the whole catalog; the AI reads the
# table in one network call, the slowest by far. Regex is a lightweight pattern check
# over the catalog's naming conventions.
METHOD_COST = {matching.SAVED: 0.05, matching.EXACT: 0.05, matching.FUZZY: 0.5, matching.SEMANTIC: 2.0,
               matching.AI: 10.0, matching.REGEX: 0.25}
METHOD_DOING = {matching.SAVED: "looking up saved mappings", matching.EXACT: "comparing exact names",
                matching.FUZZY: "comparing spellings", matching.SEMANTIC: "comparing meanings (word2vec)",
                matching.AI: "asking the AI", matching.REGEX: "checking name patterns"}
# The units the word2vec file is worth while it is read (first use only): most of its phase.
VECTOR_UNITS = 4.0
# Checking a table costs its rows (dates are parsed on each) plus a fixed part for the rules.
CHECK_BASE_ROWS = 1_000


def start(job_ids: list[str], batch_id: str | None) -> Session:
    """Check the files can be validated, then build the session on a worker thread. Its
    tracker reports how far it has got; ``events`` streams that to the browser."""
    db.require()
    jobs = []
    for job_id in dict.fromkeys(job_ids):
        job = store.job(job_id)
        if job.status != SUCCEEDED:
            raise conflict(f"{job.source_name} has not been cleaned successfully.",
                           "Only cleaned files can be validated. Run the cleaning job again.")
        if job.outputs:
            jobs.append(job)
    if not jobs:
        raise conflict("None of these files produced a table to validate.")
    session = Session(id=str(uuid.uuid4()), batch_id=batch_id, files=[], outputs=[])
    records = [record for job in jobs for record in job.outputs]
    # The counters the progress shows, at nought of their totals.
    session.tracker.count("tables_matched", 0, len(records))
    session.tracker.count("columns_matched", 0, sum(len(record.frame.columns) for record in records))
    session.tracker.count("tables_checked", 0, len(records))
    session.tracker.count("rows_checked", 0, sum(record.frame.height for record in records))
    with _sessions_lock:
        _sessions[session.id] = session
    threading.Thread(target=_run, args=(session, jobs), name=f"validate-{session.id}", daemon=True).start()
    return session


def _run(session: Session, jobs: list) -> None:
    tracker = session.tracker
    try:
        with session.lock:
            _build(session, jobs)
        tracker.succeed(lambda: jsonable_encoder(session_out(session)))
    except Exception as error:  # noqa: BLE001 - reported to the reviewer on the stream
        if isinstance(error, ApiError):
            failure = error.as_dict()
        else:
            log.exception("Validation %s failed", session.id)
            failure = {"code": "validation_failed", "message": "The validation stopped unexpectedly.",
                       "advice": "Validate again. If it keeps happening, check the server logs.",
                       "detail": "".join(traceback.format_exception_only(type(error), error)).strip()[:500],
                       "field": None}
        tracker.fail(failure)


def _build(session: Session, jobs: list) -> None:
    tracker = session.tracker
    context = _read([job.source_name for job in jobs], tracker=tracker)
    session.divisions, session.pending, session.loaded = context.divisions, context.pending, context.loaded
    session.scope_required, session.scope_by_pc = context.scope_required, context.scope_by_pc
    session.period_choices_by_pc = context.period_choices_by_pc
    session.bronze_counts = context.bronze_counts
    from . import silver_service

    catalog_columns = silver_service.catalog()
    rows = session.mapping_rows = context.mapping_rows

    # The matchers, per file (the AI is shown this profit center's precedents first).
    vectors = bool(config.WORD2VEC_PATH) and not semantic.loaded(config.WORD2VEC_PATH)
    tracker.start(MATCHERS, len(jobs) + (VECTOR_UNITS if vectors else 0),
                  "Reading the word2vec vectors" if vectors else "Setting up the matching methods")
    read_so_far = 0.0

    def on_words(read: int, wanted: int) -> None:
        nonlocal read_so_far
        units = VECTOR_UNITS * read / wanted if wanted else VECTOR_UNITS
        tracker.advance(units - read_so_far, f"Reading the word2vec vectors: {read:,} of {wanted:,} words")
        read_so_far = units

    notes: list[str] = []
    prepared = []
    for index, job in enumerate(jobs):
        tokens = file_meta.pc_tokens(job.source_name)
        combined = file_meta.multi_pc_tokens(job.source_name)
        # Several profit centers in the name -- a consolidated list (PC0005,0077,...) or
        # separately tagged (PC2018_PC0679): the first named is the file's, so it loads,
        # and its division looks up, under that one. The reviewer can change it.
        named = combined if combined and len(combined) > len(tokens) else tokens
        pc = named[0] if named else None
        received, note = file_meta.received_date_from_filename(job.source_name)
        file = FileCheck(job_id=job.id, file_name=job.source_name, file_sha256=job.source_sha256 or job.id,
                         pc_id=pc, detected_pc_id=pc, received=received, detected_received=received,
                         received_note=note)
        if len(named) > 1:
            listed = f"lists {len(named)} profit centers" if len(named) > 2 else f"names {' and '.join(named)}"
            file.warnings.append(f"The file name {listed}: using the first, {pc}, as this file's (and for its "
                                 "division). Change it if the file is for another.")
        _lookup_scope(session, file)
        _lookup_period_choices(session, file)
        _prefill_from_control(session, file)
        _lookup_division(session, file)
        session.files.append(file)
        similarity, llm, matcher_notes = column_matching.matchers(column_matching.precedents(rows, pc), on_words)
        if vectors and index == 0:
            tracker.advance(VECTOR_UNITS - read_so_far)  # read in full, or from the cache
        notes.extend(matcher_notes)
        prepared.append((job, file, similarity, llm))
        tracker.advance(1, f"Ready for {job.source_name}" if index == len(jobs) - 1 else
                        f"Setting up the matching methods for {jobs[index + 1].source_name}")

    # Matching, table by table: each method's cost per column, for the methods its file runs.
    tables = [(job, file, similarity, llm, record)
              for job, file, similarity, llm in prepared for record in job.outputs]
    costs = [sum(METHOD_COST[method] for method in matching.methods_run(similarity, llm)) * len(record.frame.columns)
             for *_, similarity, llm, record in tables]
    total_columns = sum(len(record.frame.columns) for *_, record in tables)
    tracker.start(MATCH, sum(costs))
    matched_columns = 0
    for index, (job, file, similarity, llm, record) in enumerate(tables):
        label = _table_label(file, record, len(job.outputs))
        reached: dict[str, float] = {}

        def on_method(method: str, columns_done: int, columns: int, label=label, reached=reached) -> None:
            units = METHOD_COST[method] * columns_done
            if method == matching.AI and columns_done < columns:
                doing = f"asking the AI to read {columns} columns"
            elif columns_done < columns:
                doing = f"{METHOD_DOING[method]}, {columns_done} of {columns} columns"
            else:
                doing = f"{METHOD_DOING[method]}, done"
            tracker.advance(units - reached.get(method, 0.0), f"{label}: {doing}")
            reached[method] = units

        mapping, votes, options, found_notes = _match(record, file.pc_id, rows, catalog_columns, similarity, llm,
                                                      on_method, matching_targets(session, file.scope))
        notes.extend(found_notes)
        session.outputs.append(OutputCheck(
            key=f"{job.id}.{record.id}", job_id=job.id, output_id=record.id, name=record.name,
            sheet_names=list(record.sheet_names), rows=record.frame.height, columns=_columns(record),
            mapping=mapping, votes=votes, options=options,
        ))
        matched_columns += len(record.frame.columns)
        tracker.count("tables_matched", index + 1, len(tables))
        tracker.count("columns_matched", matched_columns, total_columns)
    session.notes = list(dict.fromkeys(notes))

    # The rules, table by table: dates are parsed on every row.
    outputs = session.outputs
    total_rows = sum(output.rows for output in outputs)

    def checking(output: OutputCheck) -> str:
        return f"{_output_label(session, output)}: reporting dates and required columns"

    tracker.start(CHECK, sum(output.rows + CHECK_BASE_ROWS for output in outputs), checking(outputs[0]))
    checked = {"tables": 0, "rows": 0}

    def on_checked(output: OutputCheck) -> None:
        checked["tables"] += 1
        checked["rows"] += output.rows
        last = checked["tables"] == len(outputs)
        tracker.advance(output.rows + CHECK_BASE_ROWS,
                        "Checking every sheet of each file together" if last else checking(outputs[checked["tables"]]))
        tracker.count("tables_checked", checked["tables"], len(outputs))
        tracker.count("rows_checked", checked["rows"], total_rows)

    _evaluate(session, on_checked)


def _table_label(file: FileCheck, record: OutputRecord, tables_in_file: int) -> str:
    """How the progress names a table: its file, and its sheets when the file has several tables."""
    if tables_in_file == 1:
        return file.file_name
    return f"{file.file_name} · {_sheet_name(record.sheet_names) or record.name}"


def _output_label(session: Session, output: OutputCheck) -> str:
    file = session.file(output.job_id)
    siblings = sum(1 for other in session.outputs if other.job_id == output.job_id)
    return file.file_name if siblings == 1 else f"{file.file_name} · {_sheet_name(output.sheet_names) or output.name}"


def get(session_id: str) -> Session:
    with _sessions_lock:
        found = _sessions.get(session_id)
    if found is None:
        raise not_found("That validation")
    return found


def ready(session_id: str) -> Session:
    """The session, once it is built: it can then be edited and staged."""
    session = get(session_id)
    if session.tracker.status == progress.RUNNING:
        raise conflict("This validation is still running.", "Wait for it to finish, then try again.")
    if session.tracker.status == progress.FAILED:
        raise conflict("This validation did not finish.", "Validate the files again.")
    return session


def _record(output: OutputCheck) -> OutputRecord:
    return store.job(output.job_id).output(output.output_id)


def _columns(record: OutputRecord) -> list[dict]:
    names = naming.column_names(record.columns)
    return [
        {"original": original, "name": name, "current": record.current_name(original),
         "header": record.source_headers.get(original) or record.current_name(original),
         "excluded": original in record.excluded, "dtype": str(record.frame.schema[original])}
        for original, name in zip(record.frame.columns, names)
    ]


def _match(record: OutputRecord, pc: str | None, rows: list[tuple], catalog_columns, similarity, llm,
           on_method: matching.Progress | None = None, wanted_items: tuple[rules.Required, ...] = ()):
    """Every method's vote for every column against the whole Silver catalog -- so other
    targets absorb look-alikes (Policy Expiration Date is not policy_effective_date) -- and
    from that, the column found for each column ``wanted_items`` (the file's scope) requires."""
    names = naming.column_names(record.columns)
    originals = list(record.frame.columns)
    by_name = dict(zip(names, originals))
    headers = {name: record.source_headers.get(original) or record.current_name(original)
               for name, original in zip(names, originals)}
    own, known, one_to_many = column_matching.saved_votes(rows, [pc] if pc else [], names, headers)
    head = record.frame.head(SAMPLE_ROWS).rename(dict(zip(originals, names)))
    suggestions, notes = matching.suggest(
        names, catalog_columns, own, known, column_matching.samples(head, names), similarity, llm,
        fuzzy_min=config.MATCH_FUZZY_MIN, semantic_min=config.MATCH_SEMANTIC_MIN, one_to_many=one_to_many,
        progress=on_method)
    wanted = {item.name for item in wanted_items}
    mapping: dict[str, str | None] = {name: None for name in wanted}
    votes: dict[str, dict] = {}
    options: dict[str, list[dict]] = {name: [] for name in wanted}
    for suggestion in suggestions:
        chosen = next((c for c in suggestion.candidates if c.recommended), None)
        for target in ([suggestion.silver_column] if suggestion.silver_column else []) + list(suggestion.also):
            if target in wanted and mapping[target] is None:
                mapping[target] = by_name[suggestion.bronze_column]
                votes[target] = _vote(chosen, suggestion.reason)
        for candidate in suggestion.candidates:
            if candidate.silver_column in wanted and candidate.support:
                options[candidate.silver_column].append({
                    "column": by_name[suggestion.bronze_column], "methods": candidate.methods,
                    "score": round(float(candidate.strength), 3)})
    for name in options:
        options[name].sort(key=lambda item: -item["score"])
    return mapping, votes, options, notes


def _vote(candidate, reason: str) -> dict:
    if candidate is None:
        return {"methods": [], "method": None, "score": None, "reason": reason}
    methods = candidate.methods
    best = max(methods, key=lambda method: matching.WEIGHT.get(method, 0), default=None)
    return {"methods": methods, "method": best, "score": round(float(candidate.strength), 3), "reason": reason}


def _prefill_from_control(session: Session, file: FileCheck) -> None:
    """A file the control table already lists: its received date is the prefill, and a
    different one in the file name is pointed out."""
    row = _pending_for(session, file.source_system, file.file_name)
    if not row or not row.get("file_received_date"):
        return
    listed = row["file_received_date"]
    if file.detected_received and file.detected_received != listed:
        file.warnings.append(f"The control table lists the file as received {listed:%d %b %Y}; the file name says "
                             f"{file.detected_received:%d %b %Y}. The control table's date is used.")
    file.received = listed


def _pending_for(session: Session, source_system: str | None, file_name: str, output_id: str | None = None,
                 taken: set[int] | None = None) -> dict | None:
    """The pending control row an output fills in: its own (staged before), else one the
    business listed for the file (no output yet)."""
    if not source_system:
        return None
    rows = [row for row in session.pending if row["source_system"] == source_system and row["file_name"] == file_name]
    if output_id:
        own = next((row for row in rows if row.get("output_id") == output_id), None)
        if own:
            return own
    return next((row for row in rows if not row.get("output_id") and row["control_id"] not in (taken or set())), None)


def _lookup_division(session: Session, file: FileCheck) -> None:
    file.division_matches = file_meta.divisions_for(file.pc_id, session.divisions)
    if file.division_name not in file.division_matches:
        file.division_name = file.division_matches[0] if len(file.division_matches) == 1 else None
    note = "is not in the division table"
    file.warnings = [w for w in file.warnings if note not in w]
    if file.pc_id and session.divisions and not file.division_matches:
        file.warnings.append(f"{file.pc_id} {note}: choose a division, or division_name stays empty.")


def _lookup_scope(session: Session, file: FileCheck) -> None:
    """This profit center's scope, from its control-table rows. Never overwrites a scope the
    reviewer chose themselves (only one that matched what was detected before)."""
    previous_detected = file.detected_scope
    file.detected_scope = session.scope_by_pc.get(file.pc_id) if file.pc_id else None
    if file.scope is None or file.scope == previous_detected:
        file.scope = file.detected_scope


def _lookup_period_choices(session: Session, file: FileCheck) -> None:
    """The date basis and reporting file type this profit center's last staged file used,
    as a starting point. Like the scope, never overwrites the reviewer's own pick (only one
    that matched what was detected before)."""
    found = session.period_choices_by_pc.get(file.pc_id, {}) if file.pc_id else {}
    for name in ("date_basis", "reporting_file_type"):
        detected = f"detected_{name}"
        previous = getattr(file, detected)
        setattr(file, detected, found.get(name))
        if getattr(file, name) is None or getattr(file, name) == previous:
            setattr(file, name, getattr(file, detected))


def _rematch_file(session: Session, file: FileCheck) -> None:
    """Re-run column matching for this file's outputs: required columns depend on scope, and
    ``_match`` only ran once, in ``_build``, against whatever scope was known then. A scope
    discovered or changed afterwards (a pc_id entered or corrected, or the reviewer picking a
    different scope) would otherwise leave every output's mapping stuck at that first guess."""
    from . import silver_service

    outputs = [output for output in session.outputs if output.job_id == file.job_id and not output.staged]
    if not outputs:
        return
    catalog_columns = silver_service.catalog()
    similarity, llm, notes = column_matching.matchers(column_matching.precedents(session.mapping_rows, file.pc_id))
    session.notes = list(dict.fromkeys(session.notes + notes))
    wanted_items = matching_targets(session, file.scope)
    for output in outputs:
        previous_mapping, previous_votes = output.mapping, output.votes
        record = _record(output)
        mapping, votes, options, found_notes = _match(record, file.pc_id, session.mapping_rows, catalog_columns,
                                                       similarity, llm, None, wanted_items)
        session.notes = list(dict.fromkeys(session.notes + found_notes))
        # The reviewer's own choices survive a rematch, for whichever targets are still wanted.
        for target in output.reviewer & set(mapping):
            if target in previous_mapping:
                mapping[target] = previous_mapping[target]
                if target in previous_votes:
                    votes[target] = previous_votes[target]
        output.mapping, output.votes, output.options = mapping, votes, options
        output.fills = {target: fill for target, fill in output.fills.items() if target in mapping}


def division_options(session: Session, file: FileCheck) -> list[str]:
    if file.division_matches:
        return list(file.division_matches)
    return sorted({str(row[0]).strip() for row in session.divisions if row[0]})


# --- the rules, per output -------------------------------------------------------------


def _evaluate(session: Session, on_checked=None) -> None:
    """Every output against the rules, then every file whole. ``on_checked`` hears of each
    output as it is done. Companions come last: they take the dates of the file they came
    with."""
    taken: set[int] = set()
    for output in sorted(session.outputs, key=lambda item: item.choice == rules.COMPANION):
        file = session.file(output.job_id)
        control = _pending_for(session, file.source_system, file.file_name, output.output_id, taken)
        if control:
            taken.add(control["control_id"])
        _evaluate_one(session, file, output, control)
        if on_checked is not None:
            on_checked(output)
    for file in session.files:
        _evaluate_file(file, [output for output in session.outputs if output.job_id == file.job_id])


def _present(session: Session, output: OutputCheck) -> set[str]:
    """The required columns (Silver names) the output carries: mapped, and not left out on Results."""
    excluded = {column["original"] for column in output.columns if column["excluded"]}
    file = session.file(output.job_id)
    return {item.name for item in required_for(session, file.scope)
            if item.name in output.fills
            or (output.mapping.get(item.name) is not None and output.mapping[item.name] not in excluded)}


@dataclass
class CommissionPair:
    """One rate / amount pair of an output against its premium (``ahi_bronze.commission``)."""

    rate: str
    amount: str
    # Required column -> where its values come from: "column" or "value".
    have: dict[str, str]
    # The values, the rate as a fraction, one per row of the output's frame; None: neither.
    rates: pl.Series | None
    amounts: pl.Series | None
    premium: pl.Series
    # 0.01 when the file writes the rate as a percent (25 for 25%), else 1.
    scale: float = 1.0
    # The required column worked out at staging, from the other and the premium.
    derive: str | None = None
    agreement: commission.Agreement | None = None


def _commission_source(output: OutputCheck, name: str, frame: pl.DataFrame, excluded: set[str]) -> tuple[str, pl.Series] | None:
    """Where a rate or amount comes from: the reviewer's value, or a file column kept for Bronze."""
    fill = output.fills.get(name)
    if fill and fill.get("kind") == FILL_VALUE:
        value = commission.typed_rate(fill["value"]) if name in commission.RATES else commission.number(fill["value"])
        return "value", pl.Series([value] * frame.height, dtype=pl.Float64)
    column = output.mapping.get(name)
    if column and column not in excluded and column in frame.columns and not fill:
        return "column", commission.numbers(frame[column])
    return None


def _commission_pairs(session: Session, output: OutputCheck, frame: pl.DataFrame,
                      excluded: set[str]) -> list[CommissionPair]:
    """Each rate / amount pair the scope requires, when the premium is in the file: checked
    row by row when both are there, the other one to derive when only one is."""
    file = session.file(output.job_id)
    wanted = {item.name for item in required_for(session, file.scope)}
    premium_column = output.mapping.get(commission.PREMIUM)
    if not premium_column or premium_column in excluded or premium_column not in frame.columns:
        return []
    premium = commission.numbers(frame[premium_column])
    pairs = []
    for rate_name, amount_name in commission.PAIRS:
        if rate_name not in wanted and amount_name not in wanted:
            continue
        rate, amount = (_commission_source(output, name, frame, excluded) for name in (rate_name, amount_name))
        if rate is None and amount is None:
            continue
        pair = CommissionPair(rate_name, amount_name, {}, None, None, premium)
        if amount is not None:
            pair.have[amount_name], pair.amounts = amount
        if rate is not None:
            pair.have[rate_name], rates = rate
            if pair.have[rate_name] == "column":
                pair.scale = commission.scale(rates, pair.amounts, premium)
            pair.rates = rates * pair.scale
        if pair.rates is not None and pair.amounts is not None:
            pair.agreement = commission.check(pair.rates, pair.amounts, premium)
        else:
            # Only one of them: the other is derived, unless the reviewer set it to null.
            other = amount_name if pair.rates is not None else rate_name
            if other in wanted and not output.fills.get(other):
                pair.derive = other
        pairs.append(pair)
    return pairs


def _commission_notes(pairs: list[CommissionPair], labels: dict[str, str]) -> tuple[list[str], dict | None]:
    """The warnings the pairs raise and the check that sums them up (None: nothing to say)."""
    warnings, details, ok = [], [], True

    def label(name):
        return labels.get(name, name)

    for pair in pairs:
        rule = f"{label(pair.rate)} × Premium = {label(pair.amount)}"
        if pair.derive:
            source = pair.amount if pair.derive == pair.rate else pair.rate
            how = f"{label(pair.amount)} ÷ Premium" if pair.derive == pair.rate else f"{label(pair.rate)} × Premium"
            details.append(f"{label(pair.derive)} derived as {how}")
            if pair.derive == pair.amount and pair.scale != 1:
                details[-1] += f" ({label(source)} read as a percent)"
            continue
        agreement = pair.agreement
        if agreement is None:
            continue
        if not agreement.rows:
            details.append(f"{rule}: no row has a premium to check against")
            continue
        if agreement.disagree:
            ok = False
            details.append(f"{rule} fails on {agreement.disagree:,} of {agreement.rows:,} rows")
            row, rate, premium, expected, stated = agreement.examples[0]
            warnings.append(
                f"{label(pair.rate)} × Premium does not give {label(pair.amount)} on {agreement.disagree:,} of "
                f"{agreement.rows:,} rows (row {row}: {rate:.6g} × {premium:,.2f} = {expected:,.2f}, "
                f"but {label(pair.amount)} is {stated:,.2f}).")
        else:
            details.append(f"{rule} on all {agreement.rows:,} rows")
        if agreement.no_premium:
            details[-1] += f"; {agreement.no_premium:,} without a premium not checked"
    if not details:
        return warnings, None
    return warnings, {"id": "commission", "label": "Commission arithmetic", "ok": ok, "detail": "; ".join(details)}


def _evaluate_one(session: Session, file: FileCheck, output: OutputCheck, control: dict | None) -> None:
    record = _record(output)
    frame = record.frame
    excluded = {column["original"] for column in output.columns if column["excluded"]}
    required_items = required_for(session, file.scope)
    labels = {item.name: item.label for item in required_items}
    # A companion and the file it came with are checked together: what one lacks, the other
    # may carry (a broker list holds the producer details).
    primary = _primary(session, output)
    companions = [other for other in session.outputs
                  if other.choice == rules.COMPANION and other.companion_of == output.key]
    present = _present(session, output)
    if primary is not None:
        present |= set(primary.present) if isinstance(primary, Loaded) else _present(session, primary)
    for other in companions:
        present |= _present(session, other)
    pair = " + ".join([_label(session, output)] + ([_label(session, primary)] if primary is not None else [])
                      + [_label(session, other) for other in companions]) if primary is not None or companions else None
    missing = rules.missing_required(required_items, present) if file.scope else []
    # A column left out on Results matters only when nothing else meets its requirement.
    left_out = [item.label for group in rules.requirements(required_items) if not any(item.name in present for item in group)
                for item in group if output.mapping.get(item.name) in excluded]
    # Always every date-role column, whatever the scope itself requires: the date basis
    # is global, so _match() (matching_targets) already tried to map these regardless.
    roles = {item.date_role: output.mapping.get(item.name) for item in date_role_items()}
    stats = rules.date_stats(frame, roles)
    source = rules.reporting_source(stats)
    listed = ((control["drt_reporting_start_date"], control["drt_reporting_end_date"])
              if control and control.get("drt_reporting_start_date") and control.get("drt_reporting_end_date") else None)

    # Transactions, or the profit center's own aggregates? Told by the table's shape.
    provenance = SOURCE_SHEET_COLUMN if SOURCE_SHEET_COLUMN in frame.columns else None
    own = [name for name in frame.columns if name != provenance and name not in excluded]
    detected = grain.detect(frame, own, keyed="policy_number" in _present(session, output),
                            dated=any(stat.complete for stat in stats.values()), group=provenance)
    aggregated = output.grain == grain.AGGREGATE if output.grain else detected.aggregated
    sheet_months = None
    if aggregated:
        # Aggregates carry no transaction columns: the business's required columns are for
        # transactions. What they need is an amount; their dates come from their sheets
        # (a month to a sheet) or the file name. Entirely out of scope for date_basis /
        # reporting_file_type / data_collection_period, which are a transactional file's.
        primary, companions, pair = None, [], None
        missing = [] if detected.measures else ["an amount column"]
        left_out = []
        sheet_months = rules.sheet_months(frame[provenance].drop_nulls().unique().to_list() if provenance
                                          else output.sheet_names)

    ranges = rules.role_ranges(stats)
    start = end = detail = origin = None
    kind = computed_kind = actual_kind = None
    basis_role = basis_stat = basis_column = None
    flag_reasons: list[str] = []
    picked = output.date_role if output.date_role in ranges else None
    if aggregated:
        if picked:
            (start, end), detail, origin = rules.role_range(stats[picked]), picked, "picked"
        elif output.entered:
            # date_detail: the date column these dates are the range of, else blank.
            (start, end), origin = output.entered, "entered"
            detail = rules.matching_role(stats, start, end)
        elif output.use_control_dates and listed:
            (start, end), origin = listed, "control"
            detail = rules.matching_role(stats, start, end)
        elif sheet_months:
            first, last = min(sheet_months.values()), max(sheet_months.values())
            start = date(int(first[:4]), int(first[5:]), 1)
            end = rules.month_end(date(int(last[:4]), int(last[5:]), 1))
            origin = "sheets"
        elif file_meta.period_from_filename(file.file_name):
            first, last = file_meta.period_from_filename(file.file_name)
            start = date(int(first[:4]), int(first[5:]), 1)
            end = rules.month_end(date(int(last[:4]), int(last[5:]), 1))
            origin = "file_name"
        elif source:
            start, end = rules.role_range(source)
            detail, origin = source.role, source.role
        kind, classify_flag = rules.classify(start, end) if start else (None, None)
        if classify_flag:
            flag_reasons.append(classify_flag)
    else:
        # Manual and file-level: which date column decides, which kind of file this is, and
        # which month it reports -- together they compute the official reporting period.
        # The column's *actual* scanned shape (below) is a separate, descriptive check: it
        # never decides start/end, only the persisted period_type and the mismatch flag.
        basis_role = output.date_role or file.date_basis
        basis_stat = stats.get(basis_role) if basis_role else None
        basis_column = basis_stat.column if basis_stat else None
        detail = basis_role
        if file.reporting_file_type and file.data_collection_period:
            start, end = rules.reporting_range(file.reporting_file_type, file.data_collection_period)
            kind = computed_kind = rules.computed_period_type(file.reporting_file_type)
            origin = "computed"
        if basis_stat is not None and not basis_stat.complete:
            flag_reasons.append(f"{basis_role} is populated on {basis_stat.populated} of {basis_stat.rows} rows: "
                               "pick a different date basis, or complete the data.")
        elif basis_stat is not None and basis_stat.populated and file.reporting_file_type:
            a_start, a_end = rules.role_range(basis_stat)
            actual_kind, note = rules.actual_period_type(file.reporting_file_type, a_start, a_end)
    primary_waiting = isinstance(primary, OutputCheck) and primary.result.get("reporting_start_date") is None
    if primary is not None:
        # A companion reports what the file it came with reports.
        flag_reasons = []
        if isinstance(primary, Loaded):
            start, end, kind, detail = primary.start, primary.end, primary.period_type, primary.date_detail
        else:
            start, end = primary.result.get("reporting_start_date"), primary.result.get("reporting_end_date")
            kind = primary.result.get("computed_period_type") or primary.result.get("period_type")
            detail = primary.result.get("date_detail")
        origin = "companion"
        # A companion simply mirrors its primary: no separate actual-vs-computed split.
        actual_kind = computed_kind = kind
    # A companion's or an already-loaded file's period_type might itself be the descriptive,
    # possibly-blank kind (an "Invalid YTD" file) -- business_action always needs a clean one.
    if start is not None and kind is None:
        kind = rules.MONTHLY if (start.year, start.month) == (end.year, end.month) else rules.YTD
        if primary is not None:
            actual_kind = computed_kind = kind

    # Each row's month: from the deciding date column, or the one month a monthly file is.
    month_column = stats[detail].column if detail else None
    fixed = rules.month_key(start) if start and kind == rules.MONTHLY and not month_column else None
    if month_column is None and fixed is None:
        best = max((stat for stat in stats.values() if stat.column and stat.populated), key=lambda s: s.populated,
                   default=None)
        month_column = best.column if best else None
    by_sheet = (provenance, sheet_months) if sheet_months and provenance and not detail else None
    if by_sheet:
        month_column = fixed = None
    months = rules.month_stats(frame, rules.row_months(frame, month_column, fixed, by_sheet), roles)

    # A cumulative file's rows for a month Bronze already has, against what is there right
    # now: a mismatch is a strong sign the file is mislabeled (really incremental).
    if (primary is None and not aggregated and file.reporting_file_type == rules.CUMULATIVE
            and start and end and file.pc_id):
        old_counts = session.bronze_counts.get(file.pc_id, {})
        for month_key in rules.months_between(start, end):
            old_count = old_counts.get(month_key, 0)
            if old_count <= 0:
                continue
            new_count = (months.get(month_key) or {}).get("rows", 0)
            note = rules.mismatch(new_count, old_count)
            if note:
                flag_reasons.append(f"{month_key}: {note}")
    flag = "; ".join(flag_reasons) if flag_reasons else None

    # An incremental file's earlier months (never later ones): each checked against what
    # Bronze currently holds for that month, so the reviewer can choose, month by month,
    # whether this file's data for it is close enough to also replace, or just informational.
    lookback_months = []
    if (primary is None and not aggregated and file.reporting_file_type == rules.INCREMENTAL
            and start and end and file.pc_id):
        declared = rules.month_key(start)
        old_counts = session.bronze_counts.get(file.pc_id, {})
        for month_key in sorted(m for m in months if m < declared):
            file_rows = (months.get(month_key) or {}).get("rows", 0)
            bronze_rows = old_counts.get(month_key, 0)
            variance = rules.month_variance(file_rows, bronze_rows)
            lookback_months.append({
                "month": month_key, "file_rows": file_rows, "bronze_rows": bronze_rows,
                "percent": round(variance * 100, 1) if variance is not None else None,
                "eligible": variance is not None and abs(variance) <= 0.10,
            })

    # This profit center's files in Bronze of the same kind: aggregates against aggregates.
    loaded = [item for item in session.loaded.get(file.source_system or "", []) if item.aggregated == aggregated]
    # The loaded files a revision of these dates may name, and the one the reviewer did.
    revisable = rules.overlapping(loaded, start, end) if start else []
    revises = next((item for item in revisable if item.file_name == output.replaces_file), None) \
        if output.choice == rules.REVISE else None
    partners = _partners(session, file, output, start, end) if primary is None and not aggregated else []
    primary_rejected = isinstance(primary, OutputCheck) and primary.decision is not None \
        and primary.decision.action == rules.REJECTED
    if primary_rejected:
        decision = Decision(rules.REJECTED, [f"Rejected with {_label(session, primary)}, the file it came with."])
    elif primary_waiting:
        decision = None
    elif output.choice == rules.REJECT or missing or start:
        decision = rules.business_action(
            missing=missing, flag=flag, period_type=kind, start=start, end=end, loaded=loaded, choice=output.choice,
            revises=revises, companion=_label(session, primary) if primary is not None else None,
            partners=bool(partners), pair=pair)
    else:
        decision = None

    # The reviewer's opted-in earlier months, resolved to the loaded files holding them --
    # only still-eligible ones, in case the data changed since the reviewer picked them.
    if decision is not None and decision.action == rules.APPEND and output.replace_months:
        eligible_now = {item["month"] for item in lookback_months if item["eligible"]}
        replace_months_map: dict[str, list[int]] = {}
        for month in output.replace_months:
            if month not in eligible_now:
                continue
            ids = [item.control_id for item in loaded if month in item.months]
            if ids:
                replace_months_map[month] = ids
        if replace_months_map:
            decision.replace_months = replace_months_map
            decision.confirm = True

    # What the file continues in Bronze (the months it is appended after, or the file it
    # replaces): its columns against that file's, for the reviewer to see before Ingest.
    schema = _schema_change(output, decision)

    warnings = []
    if output.choice == rules.COMPANION and primary is None and not aggregated:
        warnings.append("The file it came with is no longer here or loaded: choose again what happens to it.")
    if output.choice == rules.REVISE and output.replaces_file and revises is None:
        warnings.append(f"{output.replaces_file} is no longer a loaded file of these reporting dates: name the file "
                        "this one revises again.")
    if aggregated and output.date_role and not picked:
        warnings.append(f"{output.date_role} is no longer populated on every row, so it cannot decide the "
                        "reporting dates.")
    if not aggregated and actual_kind is None and basis_stat is not None and basis_stat.complete \
            and file.reporting_file_type:
        _, note = rules.actual_period_type(file.reporting_file_type, *rules.role_range(basis_stat))
        if note:
            warnings.append(note)
    if left_out:
        warnings.append(f"Left out of ingestion on Results: {', '.join(left_out)}. Include it there, or map "
                        "another column.")
    if listed and start and origin not in ("entered", "control") and listed != (start, end):
        warnings.append(f"The control table lists {_span(*listed)}; the data says {_span(start, end)}.")
    pairs = [] if aggregated else _commission_pairs(session, output, frame, excluded)
    commission_warnings, commission_check = _commission_notes(pairs, labels)
    warnings += commission_warnings
    needs = []
    if not file.pc_id:
        needs.append(PC_NEEDED)
    if not file.scope:
        needs.append(SCOPE_NEEDED)
    if not aggregated and primary is None and not missing and output.choice != rules.REJECT:
        # A companion inherits its dates from the file it came with: it never needs its
        # own date basis, file type or period. Nor does a file already missing a required
        # column, or one the reviewer is rejecting outright -- it is rejected either way,
        # whatever these three say, so they are never why it is held back.
        if not file.date_basis:
            needs.append(DATE_BASIS_NEEDED)
        if not file.reporting_file_type:
            needs.append(FILE_TYPE_NEEDED)
        if not file.data_collection_period:
            needs.append(PERIOD_NEEDED)
    if decision is None and primary_waiting:
        needs.append(f"Finish {_label(session, primary)} first: a companion takes its reporting dates.")
    elif decision is None and aggregated:
        needs.append("Enter the reporting start and end dates: no date column is populated on every row.")
    elif decision is None:
        pass  # a transactional file with nothing decided yet: already explained above
    elif decision.action == rules.DECIDE:
        needs.append("Choose what happens to this file.")
    elif decision.action in (rules.INSERT, rules.APPEND) and not file.received:
        needs.append("Enter the file received date.")
    if needs:
        verdict = NEEDS_INPUT
    elif decision.action == rules.REJECTED:
        verdict = FLAGGED if (flag or decision.flagged) and not missing and output.choice != rules.REJECT else REJECTED
    else:
        verdict = READY

    needed = len(rules.requirements(required_items))
    checks = [
        {"id": "columns", "label": "Required columns", "ok": not missing and bool(file.scope),
         "detail": "Choose the scope to check required columns" if not file.scope else
         f"{needed - len(missing)} of {needed} found" + (f"; missing {', '.join(missing)}" if missing else "")},
        {"id": "dates", "label": "Reporting dates", "ok": start is not None,
         "detail": (_span(start, end) + f" · {_origin_label(origin, detail)}") if start else
         "No date column is populated on every row"},
        {"id": "grain", "label": "Aggregated or transactions", "ok": True,
         "detail": ("Aggregated: to the aggregate table" if aggregated else "Transactions: to the transaction table")
         + (" (your call)" if output.grain else "")},
        {"id": "period", "label": "Year to date or monthly", "ok": kind is not None,
         "detail": kind or flag or "Needs the reporting dates"},
        {"id": "received", "label": "File received date", "ok": file.received is not None,
         "detail": f"{file.received:%d %b %Y}" if file.received else "Not in the file name"},
        {"id": "pc", "label": "Profit center", "ok": bool(file.pc_id), "detail": file.pc_id or "Not in the file name"},
        {"id": "bronze", "label": "Against Bronze", "ok": bool(decision) and decision.action in (rules.INSERT, rules.APPEND),
         "detail": decision.action if decision else "Needs the reporting dates"},
    ]
    if commission_check:
        checks.append(commission_check)
    compare = None
    if decision and decision.overlaps and start:
        month = rules.month_key(start)
        kept = [column for column in output.columns if not column["excluded"]]
        compare = {
            "month": month,
            "this": {"file_name": file.file_name, "columns": len(kept), **_month_view(months.get(month))},
            "earlier": [{"control_id": item.control_id, "file_name": item.file_name, "columns": item.columns,
                         **_month_view(item.months.get(month))} for item in decision.overlaps],
        }
    output.decision = decision
    # The whole-file check and the fitness follow in _evaluate_file.
    output.result = {
        "verdict": verdict, "needs": needs, "warnings": warnings, "missing": missing, "checks": checks,
        # Required column -> the one it is derived from at staging (rate x premium, or amount / premium).
        "derived": {pair.derive: pair.amount if pair.derive == pair.rate else pair.rate
                    for pair in pairs if pair.derive},
        "dates": {role: stat.as_dict() for role, stat in stats.items()},
        # period_type is the actual, possibly-NULL ("Invalid YTD") shape for a transactional
        # file; computed_period_type is the clean YTD/MONTHLY the declared file type and
        # period always give, which decision-making above already used as `kind`.
        "date_detail": detail, "origin": origin, "flag": flag,
        "period_type": kind if aggregated else actual_kind, "computed_period_type": computed_kind,
        "basis_column": basis_column,
        "date_role": picked, "date_roles": ranges,
        "reporting_start_date": start, "reporting_end_date": end,
        "listed": {"start": listed[0], "end": listed[1]} if listed else None,
        "months": months, "month_column": month_column, "fixed_month": fixed,
        "action": decision.action if decision else None,
        "reasons": decision.reasons if decision else [], "options": decision.options if decision else [],
        "confirm": decision.confirm if decision else False,
        "file_replaced": decision.file_replaced() if decision else None,
        "replaces_file": revises.file_name if revises else None,
        "revisable": [{"control_id": item.control_id, "file_name": item.file_name, "start": item.start,
                       "end": item.end, "period_type": item.period_type, "rows": item.rows} for item in revisable],
        "replace_month": decision.replace_month if decision else None,
        "lookback_months": lookback_months,
        "replace_months": sorted(decision.replace_months) if decision and decision.replace_months else [],
        "compare": compare,
        "companion_of": output.companion_of if primary is not None else None,
        "companion": _partner_view(session, primary) if primary is not None else None,
        "companions": [_label(session, other) for other in companions],
        "partners": partners, "pair": pair,
        "aggregated": aggregated, "grain": {**detected.as_dict(), "override": output.grain},
        "sheet_months": sheet_months, "provenance": provenance,
        "appends_to": [{"control_id": item.control_id, "file_name": item.file_name, "start": item.start,
                        "end": item.end} for item in (decision.appends if decision else [])],
        "schema": schema,
        "control": {"control_id": control["control_id"], "seeded": not control.get("staging_table")} if control else None,
        "labels": labels,
    }


def _label(session: Session, item) -> str:
    """A file (and its sheet, when it has several outputs) as the reviewer reads it."""
    if isinstance(item, Loaded):
        return item.file_name
    file = session.file(item.job_id)
    siblings = [other for other in session.outputs if other.job_id == item.job_id]
    sheet = _sheet_name(item.sheet_names) or item.name
    return f"{file.file_name} ({sheet})" if len(siblings) > 1 else file.file_name


def _partner_view(session: Session, item) -> dict:
    if isinstance(item, Loaded):
        return {"ref": f"control:{item.control_id}", "file_name": item.file_name, "sheet_name": None, "loaded": True,
                "start": item.start, "end": item.end, "label": _label(session, item)}
    file = session.file(item.job_id)
    return {"ref": item.key, "file_name": file.file_name, "sheet_name": _sheet_name(item.sheet_names), "loaded": False,
            "start": item.result.get("reporting_start_date"), "end": item.result.get("reporting_end_date"),
            "label": _label(session, item)}


def _came_together(received: date | None, other_received: date | None, dates: tuple, other_dates: tuple) -> bool:
    """Two files of one profit center came together: the same file received date (from the
    file names), or, when either has none, the same reporting dates."""
    if received and other_received:
        return received == other_received
    return bool(dates[0]) and tuple(dates) == tuple(other_dates)


def _partners(session: Session, file: FileCheck, output: OutputCheck, start: date | None, end: date | None) -> list[dict]:
    """The files this one may be a companion of: of the same profit center, in this batch
    or loaded, that came with it. Never another companion, nor a sheet of its own file,
    nor an aggregated table (aggregates are not joined)."""
    if not file.pc_id:
        return []
    found = []
    for other in session.outputs:
        other_file = session.file(other.job_id)
        if (other.job_id == output.job_id or other.choice == rules.COMPANION or other_file.pc_id != file.pc_id
                or other.result.get("aggregated")):
            continue
        if _came_together(file.received, other_file.received, (start, end),
                          (other.result.get("reporting_start_date"), other.result.get("reporting_end_date"))):
            found.append(_partner_view(session, other))
    for item in session.loaded.get(file.source_system or "", []):
        if not item.aggregated and _came_together(file.received, item.received, (start, end), (item.start, item.end)):
            found.append(_partner_view(session, item))
    return found


def _primary(session: Session, output: OutputCheck):
    """The file a companion came with: an output of this session or a loaded file (``Loaded``)."""
    if output.choice != rules.COMPANION or not output.companion_of:
        return None
    ref = output.companion_of
    if ref.startswith("control:"):
        control_id = int(ref.split(":", 1)[1])
        return next((item for items in session.loaded.values() for item in items if item.control_id == control_id), None)
    other = next((item for item in session.outputs if item.key == ref), None)
    return other if other is not None and other.choice != rules.COMPANION else None


def _companion_ref(session: Session, output: OutputCheck, ref: str | None) -> str:
    """The file the reviewer says this one came with: one of its partners."""
    file = session.file(output.job_id)
    if any(other.companion_of == output.key and other.choice == rules.COMPANION for other in session.outputs):
        raise ApiError(422, "already_primary", f"{file.file_name} is the file another came with; it cannot be a "
                       "companion itself.", field="companion_of")
    partners = _partners(session, file, output, output.result.get("reporting_start_date"),
                         output.result.get("reporting_end_date"))
    ref = (ref or "").strip()
    if ref.isdigit():
        ref = f"control:{ref}"
    if not any(partner["ref"] == ref for partner in partners):
        names = ", ".join(partner["label"] for partner in partners)
        raise ApiError(422, "unknown_companion_of", "Choose the file it came with: one of this profit center's files "
                       "received with it.", f"Choose one of: {names}." if names else
                       "No file of this profit center came with it, so it cannot be a companion.", field="companion_of")
    return ref


def _left_out(output: OutputCheck) -> bool:
    """A sheet that is not data: rejected only for missing required columns (a lookup or
    notes sheet beside the data). It is left out on its own; its file's other sheets go on."""
    return bool(output.decision and output.decision.action == rules.REJECTED and output.result.get("missing")
                and output.choice != rules.REJECT)


def _evaluate_file(file: FileCheck, outputs: list[OutputCheck]) -> None:
    """A file is fit for Bronze whole, but for sheets that are not data. A sheet missing
    required columns is left out on its own; any other sheet rejected for itself rejects
    the file's other sheets, and a sheet still waiting for the reviewer holds the others
    back, since a file is staged whole."""
    names = _sheet_labels(outputs)
    rejected = [output for output in outputs if output.decision and output.decision.action == rules.REJECTED]
    left_out = [output for output in rejected if _left_out(output)]
    unfit = [output for output in rejected if output not in left_out]
    waiting = [output for output in outputs if output.result["verdict"] == NEEDS_INPUT]
    for output in outputs:
        result = output.result
        others_unfit = [other for other in unfit if other.key != output.key]
        others_waiting = [other for other in waiting if other.key != output.key]
        others_out = [other for other in left_out if other.key != output.key]
        if output in left_out:
            going = len(outputs) - len(rejected)
            if going and not others_unfit:
                result["warnings"].append(f"Left out on its own: the file's other {_plural(going, 'sheet')} "
                                          "still go to Bronze.")
        elif output in unfit:
            fit_siblings = len(outputs) - len(rejected)
            if fit_siblings:
                result["warnings"].append(f"While it is rejected, so are the file's other "
                                          f"{_plural(fit_siblings, 'sheet')}: a file's data sheets reach Bronze "
                                          "together or not at all.")
        elif others_unfit:
            output.decision = rules.rejected_with_file(
                {names[other.key]: " ".join(other.decision.reasons) for other in others_unfit})
            needs = [PC_NEEDED] if not file.pc_id else []
            result.update(verdict=NEEDS_INPUT if needs else REJECTED, needs=needs, action=rules.REJECTED,
                          reasons=output.decision.reasons, options=[], confirm=False, file_replaced=None,
                          replace_month=None, replace_months=[], compare=None)
            for check in result["checks"]:
                if check["id"] == "bronze":
                    check.update(ok=False, detail=rules.REJECTED)
        elif others_waiting and result["verdict"] == READY:
            result.update(verdict=NEEDS_INPUT, needs=[
                f"Finish {', '.join(names[other.key] for other in others_waiting)} first: "
                "a file's sheets are staged together."])
        if len(outputs) == 1:
            ok, detail = True, "The file's only sheet"
        elif others_unfit:
            ok, detail = False, f"Rejected with {', '.join(names[other.key] for other in others_unfit)}"
        elif others_waiting:
            ok, detail = False, f"Waiting on {', '.join(names[other.key] for other in others_waiting)}"
        elif others_out:
            out = ", ".join(names[other.key] for other in others_out)
            fit = len(outputs) - 1 - len(others_out)
            ok, detail = True, (f"The other {_plural(fit, 'sheet')} fit; {out} left out (not data)" if fit
                                else f"{out} left out (not data)")
        else:
            ok, detail = True, f"The other {_plural(len(outputs) - 1, 'sheet')} fit"
        result["checks"].append({"id": "file", "label": "Every sheet of the file", "ok": ok, "detail": detail})
        result["fitness"] = round(100 * sum(check["ok"] for check in result["checks"]) / len(result["checks"]))


def _sheet_name(sheet_names) -> str | None:
    """The control table's sheet_name: the sheet, or the sheets stacked into one table."""
    return ", ".join(dict.fromkeys(sheet_names)) or None


def _sheet_labels(outputs: list[OutputCheck]) -> dict[str, str]:
    """Each output of one file by its sheets; by its name too when two come from the same sheets."""
    plain = {output.key: _sheet_name(output.sheet_names) or output.name for output in outputs}
    twins = Counter(plain.values())
    labels = {}
    for output in outputs:
        label = plain[output.key]
        labels[output.key] = label if twins[label] == 1 else f"{label} ({output.name})"
    return labels


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _month_view(stats: dict | None) -> dict:
    stats = stats or {}
    return {"rows": stats.get("rows"), "AED": stats.get("AED"), "PED": stats.get("PED"), "TED": stats.get("TED")}


def _schema_change(output: OutputCheck, decision: Decision | None) -> dict | None:
    """This table's bronze columns against those of the latest file it continues (appended
    after, or replacing): identical, reordered, evolved (columns added or missing) or
    different. Ingest compares with the table itself again before loading."""
    if decision is None or decision.action not in (rules.INSERT, rules.APPEND):
        return None
    home = max(decision.appends or decision.replaces, key=lambda item: item.end, default=None)
    if home is None or not home.column_names:
        return None
    kept = bronze_service.ingested_columns(_record(output))
    sheet = {name for original, name in kept if original == SOURCE_SHEET_COLUMN}
    comparison = compare_schema(list(home.column_names), [name for _, name in kept], ignore=sheet)
    band = recommend_schema(comparison.common_percent, config.SCHEMA_SIMILARITY_BANDS)
    return {"against": home.file_name, "control_id": home.control_id,
            "recommendation": band.recommendation if band else None,
            "recommendation_label": band.label if band else None, **comparison.as_dict()}


def _origin_label(origin: str | None, detail: str | None = None) -> str:
    """Where the reporting dates came from, and the date column they are the range of."""
    if origin == "sheets":
        return "from the month each sheet names"
    if origin == "file_name":
        return "from the file name"
    if origin == "computed":
        return f"computed from the reporting file type and data collection period ({detail}'s shape checked)" \
            if detail else "computed from the reporting file type and data collection period"
    if origin == "picked":
        return f"from {detail}, picked by the reviewer"
    if origin in ("entered", "control"):
        source = "entered by the reviewer" if origin == "entered" else "from the control table"
        return f"{source} ({detail}'s range)" if detail else f"{source} (no date column's range)"
    return f"from {origin}" if origin else ""


def _span(start: date, end: date) -> str:
    if (start.year, start.month) == (end.year, end.month):
        return f"{start:%b %Y}"
    return f"{start:%b %Y} – {end:%b %Y}"


# --- edits ------------------------------------------------------------------------------


def _editable(output: OutputCheck) -> None:
    if output.staged:
        raise conflict(f"{output.name} is already staged.", "Start a new validation to stage it again.")


def update_file(session_id: str, job_id: str, fields: set[str], pc_id: str | None = None,
                file_received_date: str | None = None, division_name: str | None = None,
                scope: str | None = None, date_basis: str | None = None, reporting_file_type: str | None = None,
                data_collection_period: str | None = None) -> Session:
    session = ready(session_id)
    with session.lock:
        file = session.file(job_id)
        if all(output.staged for output in session.outputs if output.job_id == job_id):
            raise conflict(f"{file.file_name} is already staged.", "Start a new validation to stage it again.")
        if "pc_id" in fields:
            normalized = file_meta.normalize_pc_id(pc_id)
            if pc_id and str(pc_id).strip() and normalized is None:
                raise ApiError(422, "invalid_pc_id", "Enter the profit center as PC followed by its number, e.g. PC0796.",
                               field="pc_id")
            if normalized != file.pc_id:
                file.pc_id = normalized
                previous_scope = file.scope
                _lookup_scope(session, file)
                _lookup_period_choices(session, file)
                _lookup_division(session, file)
                if file.scope != previous_scope:
                    _rematch_file(session, file)
        if "file_received_date" in fields:
            file.received = _parse_day(file_received_date, "file_received_date")
        if "division_name" in fields:
            choice = (division_name or "").strip() or None
            if choice is not None and choice not in division_options(session, file):
                raise ApiError(422, "invalid_division", f"'{choice}' is not a division for {file.pc_id or 'this file'}.",
                               field="division_name")
            file.division_name = choice
        if "scope" in fields:
            choice = (scope or "").strip().upper() or None
            if choice is not None and choice not in rules.SCOPE_VALUES:
                raise ApiError(422, "invalid_scope", f"'{choice}' is not AJG, APD or BOTH.", field="scope")
            if choice != file.scope:
                file.scope = choice
                _rematch_file(session, file)
        if "date_basis" in fields:
            choice = (date_basis or "").strip().upper() or None
            if choice is not None and choice not in rules.DATE_BASIS_VALUES:
                raise ApiError(422, "invalid_date_basis", f"'{choice}' is not AED, TED or PED.", field="date_basis")
            file.date_basis = choice
        if "reporting_file_type" in fields:
            choice = (reporting_file_type or "").strip().upper() or None
            if choice is not None and choice not in rules.FILE_TYPE_VALUES:
                raise ApiError(422, "invalid_reporting_file_type", f"'{choice}' is not INCREMENTAL or CUMULATIVE.",
                               field="reporting_file_type")
            file.reporting_file_type = choice
        if "data_collection_period" in fields:
            file.data_collection_period = rules.month_start(_parse_month(data_collection_period,
                "data_collection_period")) if data_collection_period else None
        _evaluate(session)
    return session


def update_output(session_id: str, key: str, fields: set[str], mapping: dict[str, str | None] | None = None,
                  reporting_start_date: str | None = None, reporting_end_date: str | None = None,
                  choice: str | None = None, use_control_dates: bool | None = None,
                  date_role: str | None = None, replaces_file: str | None = None,
                  companion_of: str | None = None, grain_choice: str | None = None,
                  replace_months: list[str] | None = None,
                  fills: dict[str, dict | None] | None = None) -> Session:
    session = ready(session_id)
    with session.lock:
        output = session.output(key)
        _editable(output)
        if "mapping" in fields and mapping:
            known = {column["original"] for column in output.columns}
            file = session.file(output.job_id)
            names = {item.name for item in required_for(session, file.scope)}
            for target, column in mapping.items():
                if target not in names:
                    raise ApiError(422, "unknown_required_column", f"'{target}' is not a required column.")
                if column is not None and column not in known:
                    raise ApiError(422, "unknown_column", f"{output.name} has no column '{column}'.")
                output.mapping[target] = column
                output.fills.pop(target, None)
                output.reviewer.add(target)
                output.votes[target] = {"methods": [], "method": "reviewer", "score": None,
                                        "reason": "Chosen by the reviewer."}
        if "fills" in fields and fills:
            file = session.file(output.job_id)
            items = {item.name: item for item in required_for(session, file.scope)}
            for target, fill in fills.items():
                _set_fill(output, items, target, fill)
        if fields & {"reporting_start_date", "reporting_end_date"}:
            if not reporting_start_date and not reporting_end_date:
                output.entered = None
            else:
                start = _parse_month(reporting_start_date or reporting_end_date, "reporting_start_date")
                end = _parse_month(reporting_end_date or reporting_start_date, "reporting_end_date")
                if end < start:
                    start, end = end, start
                output.entered = (rules.month_start(start), rules.month_end(end))
                output.use_control_dates = False
            output.date_role = None
        if "use_control_dates" in fields:
            output.use_control_dates = bool(use_control_dates)
            if output.use_control_dates:
                output.entered = output.date_role = None
        if "grain" in fields:
            if grain_choice not in (None, grain.AGGREGATE, grain.TRANSACTION):
                raise ApiError(422, "invalid_grain", f"'{grain_choice}' is neither aggregate nor transaction.",
                               field="grain")
            output.grain = grain_choice
        if "date_role" in fields:
            output.date_role = _date_role(output, date_role)
            if output.date_role:
                output.entered, output.use_control_dates = None, False
        if "choice" in fields:
            if choice is not None and choice not in CHOICES:
                raise ApiError(422, "invalid_choice", f"Unknown choice '{choice}'.", field="choice")
            output.replaces_file = _revised_file(session, output, replaces_file) if choice == rules.REVISE else None
            output.companion_of = _companion_ref(session, output, companion_of) if choice == rules.COMPANION else None
            output.choice = choice
        if "replace_months" in fields:
            months = sorted(dict.fromkeys(replace_months or []))
            for month in months:
                if not re.fullmatch(r"\d{4}-\d{2}", month):
                    raise ApiError(422, "invalid_replace_month", f"'{month}' is not a YYYY-MM month.",
                                   field="replace_months")
            output.replace_months = months
        _evaluate(session)
    return session


def _revised_file(session: Session, output: OutputCheck, name: str | None) -> str:
    """The loaded file a revision replaces: named exactly, and found in the control table
    among this profit center's loaded files sharing a month with this file's dates."""
    file = session.file(output.job_id)
    name = (name or "").strip()
    start, end = output.result.get("reporting_start_date"), output.result.get("reporting_end_date")
    if start is None:
        raise ApiError(422, "revision_needs_dates", "Set this file's reporting dates first: a revision replaces a "
                       "file of the same dates.", field="replaces_file")
    candidates = rules.overlapping(session.loaded.get(file.source_system or "", []), start, end)
    if not name:
        raise ApiError(422, "revised_file_required", "Name the file this one revises.",
                       _choose_from(candidates), field="replaces_file")
    if not any(item.file_name == name for item in candidates):
        raise ApiError(422, "unknown_revised_file",
                       f"{name} is not a loaded file of {file.pc_id or 'this profit center'} for {_span(start, end)}.",
                       _choose_from(candidates), field="replaces_file")
    return name


def _choose_from(candidates: list[Loaded]) -> str:
    if not candidates:
        return "No file of this profit center is loaded for these dates, so this file is not a revision."
    return f"Name one of: {', '.join(dict.fromkeys(item.file_name for item in candidates))}."


def _date_role(output: OutputCheck, value: str | None) -> str | None:
    """The date column the reviewer picks to decide the reporting dates: one populated on
    every row (the population rule holds whoever decides)."""
    role = (value or "").strip().upper() or None
    if role is None:
        return None
    if role not in rules.DATE_ORDER:
        raise ApiError(422, "invalid_date_role", f"'{value}' is not AED, TED or PED.", field="date_role")
    stat = (output.result.get("dates") or {}).get(role) or {}
    if not stat.get("complete"):
        found = f"populated on {stat.get('populated', 0)} of {stat.get('rows', 0)} rows" if stat.get("column")             else "not mapped to a file column"
        raise ApiError(422, "date_role_incomplete", f"{role} is {found}, so it cannot decide the reporting dates.",
                       "Pick a date column populated on every row, or enter the dates.", field="date_role")
    return role


def _parse_day(value: str | None, name: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ApiError(422, "invalid_date", "Enter the date as YYYY-MM-DD.", field=name) from None


def _set_fill(output: OutputCheck, items: dict[str, rules.Required], target: str, fill: dict | None) -> None:
    """Fill a required column instead of mapping it (null, or one value), or clear the fill
    (``None``: back to missing, for a file column to be chosen again)."""
    item = items.get(target)
    if item is None:
        raise ApiError(422, "unknown_required_column", f"'{target}' is not a required column.")
    if fill is None:
        output.fills.pop(target, None)
        return
    if not fillable(item):
        if target in commission.AMOUNTS:
            rate = next(rate for rate, amount in commission.PAIRS if amount == target)
            raise ApiError(422, "not_fillable", f"{item.label} cannot be entered: it is worked out from "
                           f"{items[rate].label if rate in items else rate} × Premium.",
                           "Choose its file column, or enter the rate instead.")
        raise ApiError(422, "not_fillable", f"{item.label} must come from a column of the file.",
                       "Choose the file column that holds it.")
    kind = fill.get("kind")
    if kind == FILL_NULL:
        stored = {"kind": FILL_NULL}
        reason = "Left null by the reviewer."
    elif kind == FILL_VALUE:
        value = str(fill.get("value") or "").strip()
        if not value:
            raise ApiError(422, "empty_fill_value", f"Enter the value {item.label} should have.")
        if target in commission.RATES and commission.number(value) is None:
            raise ApiError(422, "fill_not_a_number", f"{item.label} is a rate: '{value}' is not a number.",
                           "Enter it as 12, 12% or 0.12.")
        stored = {"kind": FILL_VALUE, "value": value}
        reason = f"'{value}' on every row, entered by the reviewer."
    else:
        raise ApiError(422, "invalid_fill", f"'{kind}' is neither null nor value.")
    output.fills[target] = stored
    output.mapping[target] = None
    output.reviewer.add(target)
    output.votes[target] = {"methods": [], "method": "reviewer", "score": None, "reason": reason}


def _parse_month(value: str | None, name: str) -> date:
    text = (value or "").strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%Y-%m", 7)):
        try:
            return datetime.strptime(text[:size], fmt).date()
        except ValueError:
            continue
    raise ApiError(422, "invalid_reporting_date", "Enter the reporting dates as YYYY-MM.", field=name)


# --- staging -----------------------------------------------------------------------------


def stage(session_id: str, keys: list[str] | None, user_name: str) -> Session:
    """Write the chosen outputs to staging and the control table, in one transaction. A file
    is staged whole: choosing one of its outputs stages every one."""
    session = ready(session_id)
    with session.lock:
        files = {output.job_id for output in session.outputs if not keys or output.key in keys}
        # A companion and the file it came with (in this batch) are staged together.
        linked = {output.key: output for output in session.outputs}
        for output in session.outputs:
            primary = linked.get(output.companion_of or "") if output.choice == rules.COMPANION else None
            if primary is not None and (output.job_id in files or primary.job_id in files):
                files |= {output.job_id, primary.job_id}
        # The file a companion came with first, so its control row exists to point at.
        targets = sorted((output for output in session.outputs if output.job_id in files and not output.staged),
                         key=lambda item: item.choice == rules.COMPANION)
        if not targets:
            raise conflict("Nothing to stage: these tables are already staged.")
        with db.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('ahi-bronze'))")
            # Under the lock, against the control table as it is now.
            context = _read([file.file_name for file in session.files], conn)
            session.pending, session.loaded = context.pending, context.loaded
            session.bronze_counts = context.bronze_counts
            _evaluate(session)
            waiting = [output for output in targets if output.result["verdict"] == NEEDS_INPUT]
            if waiting:
                first = waiting[0]
                raise ApiError(409, "validation_not_ready", f"{first.name} can't be staged yet.",
                               " ".join(first.result["needs"]))
            staged: dict[str, dict] = {}
            for output in targets:
                staged[output.key] = _stage_one(conn, session, output, user_name, _companion_control(output, staged))
        for output in targets:
            output.staged = staged[output.key]
        with db.connection() as conn:
            context = _read([file.file_name for file in session.files], conn)
        session.pending, session.loaded = context.pending, context.loaded
        session.bronze_counts = context.bronze_counts
    return session


def _companion_control(output: OutputCheck, staged: dict[str, dict]) -> int | None:
    """The control row of the file a companion came with: loaded, or staged just before it."""
    ref = output.result.get("companion_of")
    if not ref or output.decision is None or output.decision.action == rules.REJECTED:
        return None
    if ref.startswith("control:"):
        return int(ref.split(":", 1)[1])
    return (staged.get(ref) or {}).get("control_id")


def _stage_one(conn, session: Session, output: OutputCheck, user_name: str, companion_of: int | None = None) -> dict:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    file = session.file(output.job_id)
    record = _record(output)
    result, decision = output.result, output.decision
    action = decision.action
    kept = bronze_service.ingested_columns(record)
    by_original = {column["original"]: column for column in output.columns}
    extras = _extra_columns(session, output, record)
    extra_of = {target: (name, how) for name, _, target, how in extras}

    if action in (rules.INSERT, rules.APPEND):
        _save_mapping(conn, file, output, by_original, user_name)

    control = sql.SQL("{}.{}").format(sql.Identifier(config.CONTROL_SCHEMA), sql.Identifier("control_table"))
    details = {
        "columns": [{"name": name, "header": by_original[original]["header"],
                     "datatype": by_original[original]["dtype"]} for original, name in kept]
        + [{"name": name, "header": _label_of(session, output, target), "datatype": "String"}
           for name, _, target, _ in extras],
        "provenance": next((name for original, name in kept if original == SOURCE_SHEET_COLUMN), None),
        "regions": list(record.tables), "rows": record.frame.height, "sheets": list(record.sheet_names),
        "mapping": {target: _staged_mapping(output, target, column, by_original, extra_of)
                    for target, column in output.mapping.items()},
        "dates": result["dates"], "months": result["months"], "checks": result["checks"],
        "fitness": result["fitness"], "reasons": result["reasons"], "warnings": result["warnings"],
        "replaces": [item.control_id for item in decision.replaces],
        "overlaps": [item.control_id for item in decision.overlaps],
        "replace_months": decision.replace_months,
        "output_name": output.name,
        "companion_of": companion_of,
        "aggregated": result["aggregated"], "sheet_months": result["sheet_months"],
        "appends_to": [item["control_id"] for item in result["appends_to"]],
        "schema": result["schema"],
    }
    values = {
        "source_system": file.source_system, "file_name": file.file_name,
        "sheet_name": _sheet_name(record.sheet_names),
        "reporting_period_type": result["period_type"] if action != rules.REJECTED else None,
        "processing_action": action, "bronze_load_flag": "N",
        "file_received_date": file.received,
        "drt_reporting_start_date": result["reporting_start_date"],
        "drt_reporting_end_date": result["reporting_end_date"],
        "date_detail": result["date_detail"], "file_replaced": result["file_replaced"],
        "is_active": "N" if action == rules.REJECTED else "Y",
        "pc_id": file.pc_id, "division_name": file.division_name, "scope": file.scope,
        "date_basis": file.date_basis, "reporting_file_type": file.reporting_file_type,
        "data_collection_period": file.data_collection_period, "file_sha256": file.file_sha256,
        "job_id": output.job_id, "output_id": output.output_id, "sheet_names": list(record.sheet_names),
        "rejection_reason": " ".join(decision.reasons) if action == rules.REJECTED else None,
        "validation": Jsonb(details), "replace_month": result["replace_month"], "created_by": user_name,
        "companion_of": companion_of, "is_aggregated": "Y" if result["aggregated"] else "N",
    }
    existing = result["control"]["control_id"] if result["control"] else None
    names = list(values)
    if existing is not None:
        conn.execute(sql.SQL("UPDATE {} SET {}, staged_at = now() WHERE control_id = %s").format(
            control, sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in names)),
            [values[name] for name in names] + [existing])
        control_id = existing
    else:
        control_id = conn.execute(sql.SQL("INSERT INTO {} ({}, staged_at) VALUES ({}, now()) RETURNING control_id").format(
            control, sql.SQL(", ").join(sql.Identifier(name) for name in names),
            sql.SQL(", ").join(sql.Placeholder() for _ in names)), [values[name] for name in names]).fetchone()[0]

    table = _staging_name(conn, file, record, result, control_id)
    _write_staging(conn, table, record, kept, result, extras)
    conn.execute(sql.SQL("UPDATE {} SET staging_table = %s WHERE control_id = %s").format(control), [table, control_id])
    return {"control_id": control_id, "staging_table": table, "processing_action": action,
            "seeded": bool(result["control"] and result["control"]["seeded"])}


def _staging_name(conn, file: FileCheck, record: OutputRecord, result: dict, control_id: int) -> str:
    """The bronze table this output is bound for (as Ingest will suggest it), with ``_stg``;
    never a staging table another control row still waits to load from."""
    from psycopg import sql

    start, end = result["reporting_start_date"], result["reporting_end_date"]
    years = set(range(start.year, end.year + 1)) if start and end else None
    bronze = naming.table_name(file_meta.source_system_for(file.pc_id) or "", list(record.sheet_names), years=years,
                               aggregated=result["aggregated"])
    held = {row[0] for row in conn.execute(sql.SQL(
        "SELECT staging_table FROM {}.{} WHERE control_id <> %s AND bronze_load_flag = 'N' "
        "AND staging_table IS NOT NULL AND processing_action IN ('INSERT', 'APPEND')").format(
            sql.Identifier(config.CONTROL_SCHEMA), sql.Identifier("control_table")), [control_id])}
    return naming.staging_table(bronze, held)


def _save_mapping(conn, file: FileCheck, output: OutputCheck, by_original: dict, user_name: str) -> None:
    """Every required column's file column into the bronze column mapping, where missing."""
    from psycopg import sql

    if not file.pc_id:
        return
    table = sql.SQL("{}.{}").format(sql.Identifier(config.BRONZE_SCHEMA), sql.Identifier(reference_service.MAPPING_TABLE))
    for target, column in output.mapping.items():
        if column is None:
            continue
        method = output.votes.get(target, {}).get("method") or "reviewer"
        conn.execute(sql.SQL(
            "INSERT INTO {} (profit_center, file_columns, silver_column_name, method, created_by) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING").format(table),
            [file.pc_id, by_original[column]["header"], target, method, user_name])


def _label_of(session: Session, output: OutputCheck, target: str) -> str:
    file = session.file(output.job_id)
    return next((item.label for item in required_for(session, file.scope) if item.name == target), target)


def _extra_columns(session: Session, output: OutputCheck,
                   record: OutputRecord) -> list[tuple[str, str | pl.Series, str, str]]:
    """(staged column, values, required column, how) for each required column that is not
    a column of the file: a value the reviewer typed ("value", one for every row; a rate as
    a fraction) or one derived from its commission pair ("derived", row by row). Each is a
    column of its own, named for the required column (never the name of one of the file's
    columns), so Bronze carries it and Silver matches it by name."""
    taken = set(naming.column_names(record.columns))
    wanted = {item.name for item in required_for(session, session.file(output.job_id).scope)}

    def named(target):
        name, number = target, 1
        while name in taken:
            number += 1
            name = f"{target}_{number}"
        taken.add(name)
        return name

    found = []
    for target, fill in output.fills.items():
        if fill.get("kind") != FILL_VALUE or target not in wanted:
            continue
        value = fill["value"]
        if target in commission.RATES:
            value = f"{round(commission.typed_rate(value), 6):g}"
        found.append((named(target), value, target, "value"))
    if not output.result.get("aggregated"):
        excluded = {column["original"] for column in output.columns if column["excluded"]}
        for pair in _commission_pairs(session, output, record.frame, excluded):
            if pair.derive == pair.amount:
                found.append((named(pair.amount), commission.derive_amount(pair.rates, pair.premium), pair.amount, "derived"))
            elif pair.derive == pair.rate:
                found.append((named(pair.rate), commission.derive_rate(pair.amounts, pair.premium), pair.rate, "derived"))
    return found


def _staged_mapping(output: OutputCheck, target: str, column: str | None, by_original: dict,
                    extra_of: dict[str, tuple[str, str]]) -> dict | None:
    """What the control row records for one required column: the file column, the column
    made for it (a typed value, or derived from its commission pair), the reviewer's null,
    or None (missing)."""
    fill = output.fills.get(target)
    name, how = extra_of.get(target, (None, None))
    if how == "value" and fill:
        return {"column": name, "header": None, "method": "value", "value": fill["value"]}
    if how == "derived":
        return {"column": name, "header": None, "method": "derived",
                "from": (output.result.get("derived") or {}).get(target)}
    if fill and fill.get("kind") == FILL_NULL:
        return {"column": None, "header": None, "method": "null"}
    if column:
        return {"column": by_original[column]["name"], "header": by_original[column]["header"],
                "method": output.votes.get(target, {}).get("method")}
    return None


def _write_staging(conn, table: str, record: OutputRecord, kept: list[tuple[str, str]], result: dict,
                   extras: list[tuple[str, str | pl.Series, str, str]] = ()) -> None:
    """The output's kept columns as text, each row's sheet, and each row's month -- only
    rows whose date basis column falls in the declared reporting period (the file may
    carry extra rows outside it, for a cumulative file as much as an incremental one)."""
    from psycopg import sql

    schema = sql.Identifier(config.STAGING_SCHEMA)
    target = sql.Identifier(table)
    basis_column = result.get("basis_column")
    start, end = result.get("reporting_start_date"), result.get("reporting_end_date")
    mask = None
    if basis_column and basis_column in record.frame.columns and start and end:
        mask = rules.in_range(rules.parse_dates(record.frame[basis_column]), start, end)
    source = record.frame.filter(mask) if mask is not None else record.frame
    frame = bronze_service._as_text(record, rows=mask)
    if extras:
        # A typed value on every row; a derived column's rows kept as the file's are.
        frame = frame.with_columns([
            (values.filter(mask) if mask is not None else values).alias(name) if isinstance(values, pl.Series)
            else pl.lit(values, dtype=pl.String).alias(name)
            for name, values, _, _ in extras])
    sheets = list(dict.fromkeys(record.sheet_names))
    if SOURCE_SHEET_COLUMN in source.columns:
        sheet = source[SOURCE_SHEET_COLUMN].cast(pl.String)
    else:
        sheet = pl.Series([sheets[0] if len(sheets) == 1 else None] * source.height, dtype=pl.String)
    sheet_months_arg = (SOURCE_SHEET_COLUMN, result["sheet_months"]) if result.get("sheet_months") and \
        result.get("provenance") and not result.get("date_detail") else None
    month = rules.row_months(source, result["month_column"], result["fixed_month"], sheet_months_arg)
    frame = frame.with_columns(sheet.alias("_source_sheet"), month.alias("_reporting_month"))
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
    conn.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(schema, target))
    conn.execute(sql.SQL("CREATE TABLE {}.{} ({})").format(schema, target, sql.SQL(", ").join(
        sql.SQL("{} text").format(sql.Identifier(name)) for name in frame.columns)))
    with conn.cursor() as cursor, cursor.copy(sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
            schema, target, sql.SQL(", ").join(sql.Identifier(name) for name in frame.columns))) as copy:
        for row in frame.iter_rows():
            copy.write_row(row)


# --- the API's view ------------------------------------------------------------------


def _reads_as(name: str, fill: dict | None) -> str | None:
    if name not in commission.RATES or not fill or fill.get("kind") != FILL_VALUE:
        return None
    rate = commission.typed_rate(fill["value"])
    return None if rate is None else f"{rate * 100:.6g}%"


def session_out(session: Session) -> dict:
    files = []
    for file in session.files:
        files.append({
            "job_id": file.job_id, "file_name": file.file_name, "pc_id": file.pc_id,
            "detected_pc_id": file.detected_pc_id, "source_system": file.source_system,
            "file_received_date": file.received, "detected_received_date": file.detected_received,
            "received_note": file.received_note, "division_name": file.division_name,
            "division_matches": file.division_matches, "division_options": division_options(session, file),
            "scope": file.scope, "detected_scope": file.detected_scope, "scope_options": list(rules.SCOPE_VALUES),
            "date_basis": file.date_basis, "detected_date_basis": file.detected_date_basis,
            "date_basis_options": list(rules.DATE_BASIS_VALUES),
            "reporting_file_type": file.reporting_file_type,
            "detected_reporting_file_type": file.detected_reporting_file_type,
            "reporting_file_type_options": list(rules.FILE_TYPE_VALUES),
            "data_collection_period": rules.month_key(file.data_collection_period)
                if file.data_collection_period else None,
            "warnings": file.warnings,
        })
    outputs = []
    for output in session.outputs:
        result = dict(output.result)
        scoped = required_for(session, session.file(output.job_id).scope)
        outputs.append({
            "key": output.key, "job_id": output.job_id, "output_id": output.output_id, "name": output.name,
            "sheet_names": output.sheet_names, "rows": output.rows, "columns": output.columns,
            "sheet_name": _sheet_name(output.sheet_names),
            "required": [
                {"name": item.name, "label": item.label, "date_role": item.date_role,
                 # The required columns that can stand in for this one (Silver names).
                 "one_of": [other.name for other in scoped if item.one_of and other.one_of == item.one_of
                            and other.name != item.name],
                 "column": output.mapping.get(item.name), "vote": output.votes.get(item.name),
                 "options": output.options.get(item.name, []), "reviewer": item.name in output.reviewer,
                 # The reviewer's null or value in place of a file column; whether it may have one.
                 "fill": output.fills.get(item.name), "fillable": fillable(item),
                 # Worked out at staging from this required column (rate x premium, amount / premium).
                 "derived_from": (output.result.get("derived") or {}).get(item.name),
                 # A typed rate, as it is read: "12%".
                 "reads_as": _reads_as(item.name, output.fills.get(item.name))}
                for item in scoped
            ],
            "entered": bool(output.entered), "use_control_dates": output.use_control_dates,
            "choice": output.choice, "staged": output.staged,
            **{name: value for name, value in result.items() if name not in ("labels",)},
        })
    counts = {verdict: sum(1 for output in outputs if output["verdict"] == verdict)
              for verdict in (READY, NEEDS_INPUT, FLAGGED, REJECTED)}
    return {"id": session.id, "batch_id": session.batch_id, "status": "ready", "files": files, "outputs": outputs,
            "notes": session.notes, "counts": counts, "created_at": session.created_at,
            "staged": sum(1 for output in session.outputs if output.staged)}


def session_view(session: Session) -> dict:
    """What the API shows of a session: the review once it is built, else how far building
    it has got (``running``) or why it stopped (``failed``)."""
    tracker = session.tracker
    if tracker.status == progress.SUCCEEDED:
        return session_out(session)
    _, state = tracker.snapshot()
    return {"id": session.id, "batch_id": session.batch_id, "status": tracker.status, "progress": state,
            "error": tracker.error}


def control_rows(source_system: str | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
    """The control table, newest first: every business column, then what the app adds."""
    from psycopg import sql

    where, params = [], []
    if source_system:
        where.append(sql.SQL("source_system = %s"))
        params.append(source_system.strip().upper())
    if status == "pending":
        where.append(sql.SQL("bronze_load_flag = 'N' AND staging_table IS NOT NULL "
                             "AND processing_action IN ('INSERT', 'APPEND')"))
    elif status == "loaded":
        where.append(sql.SQL("bronze_load_flag = 'Y'"))
    elif status == "rejected":
        where.append(sql.SQL("processing_action = 'REJECTED'"))
    elif status == "listed":
        where.append(sql.SQL("staging_table IS NULL AND bronze_load_flag = 'N'"))
    query = sql.SQL("SELECT {} FROM {}.{} {} ORDER BY control_id DESC LIMIT %s").format(
        sql.SQL(", ").join(sql.Identifier(name) for name in _CONTROL_FIELDS),
        sql.Identifier(config.CONTROL_SCHEMA), sql.Identifier("control_table"),
        sql.SQL("WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL(""))
    with db.connection() as conn:
        rows = conn.execute(query, params + [limit]).fetchall()
    result = []
    for row in rows:
        data = dict(zip(_CONTROL_FIELDS, row))
        details = data.pop("validation") or {}
        data["fitness"] = details.get("fitness")
        data["reasons"] = details.get("reasons") or []
        data["rows"] = details.get("rows")
        data["output_name"] = details.get("output_name")
        for name in ("created_at", "staged_at", "loaded_at"):
            data[name] = data[name].timestamp() if data[name] else None
        result.append(data)
    return result
