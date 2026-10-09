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

from ahi_bronze import file_meta, grain, naming
from ahi_bronze import validation as rules
from ahi_bronze.schema_compare import compare as compare_schema
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

log = logging.getLogger("ahi.validation")


@lru_cache(maxsize=1)
def required() -> tuple[rules.Required, ...]:
    return tuple(rules.load_required(config.BRONZE_REQUIRED_COLUMNS_FILE))


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


_CONTROL_FIELDS = ("control_id", "source_system", "file_name", "sheet_name", "reporting_period_type",
                   "processing_action", "bronze_load_flag", "file_received_date", "drt_reporting_start_date",
                   "drt_reporting_end_date",
                   "date_detail", "file_replaced", "is_active", "pc_id", "division_name", "job_id", "output_id",
                   "staging_table", "ingestion_id", "rejection_reason", "validation", "replace_month",
                   "created_by", "created_at", "staged_at", "loaded_at", "bronze_table", "companion_of", "is_aggregated")


READ_STEPS = 4


def _read(file_names: list[str], conn=None, tracker: progress.Tracker | None = None) -> Context:
    """The division table, the bronze column mapping, these files' pending control rows,
    and every profit center's files in Bronze (on ``conn`` when given). ``tracker`` is told
    of connecting (the CONNECT phase, when there is no ``conn``) and of each of the four
    reads (READ)."""
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
# table in one network call, the slowest by far.
METHOD_COST = {matching.SAVED: 0.05, matching.EXACT: 0.05, matching.FUZZY: 0.5, matching.SEMANTIC: 2.0,
               matching.AI: 10.0}
METHOD_DOING = {matching.SAVED: "looking up saved mappings", matching.EXACT: "comparing exact names",
                matching.FUZZY: "comparing spellings", matching.SEMANTIC: "comparing meanings (word2vec)",
                matching.AI: "asking the AI"}
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
    from . import silver_service

    catalog_columns = silver_service.catalog()
    rows = context.mapping_rows

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
        pc = tokens[0] if len(tokens) == 1 else None
        received, note = file_meta.received_date_from_filename(job.source_name)
        file = FileCheck(job_id=job.id, file_name=job.source_name, file_sha256=job.source_sha256 or job.id,
                         pc_id=pc, detected_pc_id=pc, received=received, detected_received=received,
                         received_note=note)
        if len(tokens) > 1:
            file.warnings.append(f"The file name names {' and '.join(tokens)}: enter the profit center this file is for.")
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
                                                      on_method)
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
           on_method: matching.Progress | None = None):
    """Every method's vote for every column against the whole Silver catalog -- so other
    targets absorb look-alikes (Policy Expiration Date is not policy_effective_date) -- and
    from that, the column found for each required one."""
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
    wanted = {item.name for item in required()}
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


def _present(output: OutputCheck) -> set[str]:
    """The required columns (Silver names) the output carries: mapped, and not left out on Results."""
    excluded = {column["original"] for column in output.columns if column["excluded"]}
    return {item.name for item in required()
            if output.mapping.get(item.name) is not None and output.mapping[item.name] not in excluded}


def _evaluate_one(session: Session, file: FileCheck, output: OutputCheck, control: dict | None) -> None:
    record = _record(output)
    frame = record.frame
    excluded = {column["original"] for column in output.columns if column["excluded"]}
    labels = {item.name: item.label for item in required()}
    # A companion and the file it came with are checked together: what one lacks, the other
    # may carry (a broker list holds the producer details).
    primary = _primary(session, output)
    companions = [other for other in session.outputs
                  if other.choice == rules.COMPANION and other.companion_of == output.key]
    present = _present(output)
    if primary is not None:
        present |= set(primary.present) if isinstance(primary, Loaded) else _present(primary)
    for other in companions:
        present |= _present(other)
    pair = " + ".join([_label(session, output)] + ([_label(session, primary)] if primary is not None else [])
                      + [_label(session, other) for other in companions]) if primary is not None or companions else None
    missing = rules.missing_required(required(), present)
    # A column left out on Results matters only when nothing else meets its requirement.
    left_out = [item.label for group in rules.requirements(required()) if not any(item.name in present for item in group)
                for item in group if output.mapping.get(item.name) in excluded]
    roles = {item.date_role: output.mapping.get(item.name) for item in required() if item.date_role}
    stats = rules.date_stats(frame, roles)
    source = rules.reporting_source(stats)
    listed = ((control["drt_reporting_start_date"], control["drt_reporting_end_date"])
              if control and control.get("drt_reporting_start_date") and control.get("drt_reporting_end_date") else None)

    # Transactions, or the profit center's own aggregates? Told by the table's shape.
    provenance = SOURCE_SHEET_COLUMN if SOURCE_SHEET_COLUMN in frame.columns else None
    own = [name for name in frame.columns if name != provenance and name not in excluded]
    detected = grain.detect(frame, own, keyed="policy_number" in _present(output),
                            dated=any(stat.complete for stat in stats.values()), group=provenance)
    aggregated = output.grain == grain.AGGREGATE if output.grain else detected.aggregated
    sheet_months = None
    if aggregated:
        # Aggregates carry no transaction columns: the business's required columns are for
        # transactions. What they need is an amount; their dates come from their sheets
        # (a month to a sheet) or the file name.
        primary, companions, pair = None, [], None
        missing = [] if detected.measures else ["an amount column"]
        left_out = []
        sheet_months = rules.sheet_months(frame[provenance].drop_nulls().unique().to_list() if provenance
                                          else output.sheet_names)

    ranges = rules.role_ranges(stats)
    start = end = None
    detail = origin = None
    picked = output.date_role if output.date_role in ranges else None
    if picked:
        (start, end), detail, origin = rules.role_range(stats[picked]), picked, "picked"
    elif output.entered:
        # date_detail: the date column these dates are the range of, else blank.
        (start, end), origin = output.entered, "entered"
        detail = rules.matching_role(stats, start, end)
    elif output.use_control_dates and listed:
        (start, end), origin = listed, "control"
        detail = rules.matching_role(stats, start, end)
    elif aggregated and sheet_months:
        first, last = min(sheet_months.values()), max(sheet_months.values())
        start = date(int(first[:4]), int(first[5:]), 1)
        end = rules.month_end(date(int(last[:4]), int(last[5:]), 1))
        origin = "sheets"
    elif aggregated and file_meta.period_from_filename(file.file_name):
        first, last = file_meta.period_from_filename(file.file_name)
        start = date(int(first[:4]), int(first[5:]), 1)
        end = rules.month_end(date(int(last[:4]), int(last[5:]), 1))
        origin = "file_name"
    elif source:
        start, end = rules.role_range(source)
        detail, origin = source.role, source.role
    kind, flag = rules.classify(start, end) if start else (None, None)
    primary_waiting = isinstance(primary, OutputCheck) and primary.result.get("reporting_start_date") is None
    if primary is not None:
        # A companion reports what the file it came with reports.
        if isinstance(primary, Loaded):
            start, end, kind, detail = primary.start, primary.end, primary.period_type, primary.date_detail
        else:
            start, end = primary.result.get("reporting_start_date"), primary.result.get("reporting_end_date")
            kind, detail = primary.result.get("period_type"), primary.result.get("date_detail")
        origin, flag = "companion", None

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

    # What the file continues in Bronze (the months it is appended after, or the file it
    # replaces): its columns against that file's, for the reviewer to see before Ingest.
    schema = _schema_change(output, decision)

    warnings = []
    if output.choice == rules.COMPANION and primary is None and not aggregated:
        warnings.append("The file it came with is no longer here or loaded: choose again what happens to it.")
    if output.choice == rules.REVISE and output.replaces_file and revises is None:
        warnings.append(f"{output.replaces_file} is no longer a loaded file of these reporting dates: name the file "
                        "this one revises again.")
    if output.date_role and not picked:
        warnings.append(f"{output.date_role} is no longer populated on every row, so it cannot decide the "
                        "reporting dates.")
    if left_out:
        warnings.append(f"Left out of ingestion on Results: {', '.join(left_out)}. Include it there, or map "
                        "another column.")
    if listed and start and origin not in ("entered", "control") and listed != (start, end):
        warnings.append(f"The control table lists {_span(*listed)}; the data says {_span(start, end)}.")
    needs = []
    if not file.pc_id:
        needs.append(PC_NEEDED)
    if decision is None and primary_waiting:
        needs.append(f"Finish {_label(session, primary)} first: a companion takes its reporting dates.")
    elif decision is None:
        needs.append("Enter the reporting start and end dates: no date column is populated on every row.")
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

    needed = len(rules.requirements(required()))
    checks = [
        {"id": "columns", "label": "Required columns", "ok": not missing,
         "detail": f"{needed - len(missing)} of {needed} found" + (f"; missing {', '.join(missing)}" if missing else "")},
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
        "dates": {role: stat.as_dict() for role, stat in stats.items()},
        "date_detail": detail, "origin": origin, "flag": flag, "period_type": kind,
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
                          replace_month=None, compare=None)
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
    return {"against": home.file_name, "control_id": home.control_id, **comparison.as_dict()}


def _origin_label(origin: str | None, detail: str | None = None) -> str:
    """Where the reporting dates came from, and the date column they are the range of."""
    if origin == "sheets":
        return "from the month each sheet names"
    if origin == "file_name":
        return "from the file name"
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
                file_received_date: str | None = None, division_name: str | None = None) -> Session:
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
                _lookup_division(session, file)
        if "file_received_date" in fields:
            file.received = _parse_day(file_received_date, "file_received_date")
        if "division_name" in fields:
            choice = (division_name or "").strip() or None
            if choice is not None and choice not in division_options(session, file):
                raise ApiError(422, "invalid_division", f"'{choice}' is not a division for {file.pc_id or 'this file'}.",
                               field="division_name")
            file.division_name = choice
        _evaluate(session)
    return session


def update_output(session_id: str, key: str, fields: set[str], mapping: dict[str, str | None] | None = None,
                  reporting_start_date: str | None = None, reporting_end_date: str | None = None,
                  choice: str | None = None, use_control_dates: bool | None = None,
                  date_role: str | None = None, replaces_file: str | None = None,
                  companion_of: str | None = None, grain_choice: str | None = None) -> Session:
    session = ready(session_id)
    with session.lock:
        output = session.output(key)
        _editable(output)
        if "mapping" in fields and mapping:
            known = {column["original"] for column in output.columns}
            names = {item.name for item in required()}
            for target, column in mapping.items():
                if target not in names:
                    raise ApiError(422, "unknown_required_column", f"'{target}' is not a required column.")
                if column is not None and column not in known:
                    raise ApiError(422, "unknown_column", f"{output.name} has no column '{column}'.")
                output.mapping[target] = column
                output.reviewer.add(target)
                output.votes[target] = {"methods": [], "method": "reviewer", "score": None,
                                        "reason": "Chosen by the reviewer."}
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

    if action in (rules.INSERT, rules.APPEND):
        _save_mapping(conn, file, output, by_original, user_name)

    control = sql.SQL("{}.{}").format(sql.Identifier(config.CONTROL_SCHEMA), sql.Identifier("control_table"))
    details = {
        "columns": [{"name": name, "header": by_original[original]["header"],
                     "datatype": by_original[original]["dtype"]} for original, name in kept],
        "provenance": next((name for original, name in kept if original == SOURCE_SHEET_COLUMN), None),
        "regions": list(record.tables), "rows": record.frame.height, "sheets": list(record.sheet_names),
        "mapping": {target: ({"column": by_original[column]["name"], "header": by_original[column]["header"],
                              "method": output.votes.get(target, {}).get("method")} if column else None)
                    for target, column in output.mapping.items()},
        "dates": result["dates"], "months": result["months"], "checks": result["checks"],
        "fitness": result["fitness"], "reasons": result["reasons"], "warnings": result["warnings"],
        "replaces": [item.control_id for item in decision.replaces],
        "overlaps": [item.control_id for item in decision.overlaps],
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
        "pc_id": file.pc_id, "division_name": file.division_name, "file_sha256": file.file_sha256,
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
    _write_staging(conn, table, record, kept, result)
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


def _write_staging(conn, table: str, record: OutputRecord, kept: list[tuple[str, str]], result: dict) -> None:
    """The output's kept columns as text, each row's sheet, and each row's month."""
    from psycopg import sql

    schema = sql.Identifier(config.STAGING_SCHEMA)
    target = sql.Identifier(table)
    frame = bronze_service._as_text(record)
    sheets = list(dict.fromkeys(record.sheet_names))
    if SOURCE_SHEET_COLUMN in record.frame.columns:
        sheet = record.frame[SOURCE_SHEET_COLUMN].cast(pl.String)
    else:
        sheet = pl.Series([sheets[0] if len(sheets) == 1 else None] * record.frame.height, dtype=pl.String)
    sheets = (SOURCE_SHEET_COLUMN, result["sheet_months"]) if result.get("sheet_months") and \
        result.get("provenance") and not result.get("date_detail") else None
    month = rules.row_months(record.frame, result["month_column"], result["fixed_month"], sheets)
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


def session_out(session: Session) -> dict:
    files = []
    for file in session.files:
        files.append({
            "job_id": file.job_id, "file_name": file.file_name, "pc_id": file.pc_id,
            "detected_pc_id": file.detected_pc_id, "source_system": file.source_system,
            "file_received_date": file.received, "detected_received_date": file.detected_received,
            "received_note": file.received_note, "division_name": file.division_name,
            "division_matches": file.division_matches, "division_options": division_options(session, file),
            "warnings": file.warnings,
        })
    outputs = []
    for output in session.outputs:
        result = dict(output.result)
        outputs.append({
            "key": output.key, "job_id": output.job_id, "output_id": output.output_id, "name": output.name,
            "sheet_names": output.sheet_names, "rows": output.rows, "columns": output.columns,
            "sheet_name": _sheet_name(output.sheet_names),
            "required": [
                {"name": item.name, "label": item.label, "date_role": item.date_role,
                 # The required columns that can stand in for this one (Silver names).
                 "one_of": [other.name for other in required() if item.one_of and other.one_of == item.one_of
                            and other.name != item.name],
                 "column": output.mapping.get(item.name), "vote": output.votes.get(item.name),
                 "options": output.options.get(item.name, []), "reviewer": item.name in output.reviewer}
                for item in required()
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
