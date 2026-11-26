"""Job history in the app database: one row per output of a run, sharing its job_id.

Clean, Bronze ingest and Silver load all report here the same way:

* ``begin`` -- a placeholder row (no output yet) as soon as the run exists;
* ``note`` -- timestamped log lines, kept in memory;
* ``update`` -- status / start time, flushing the log so far;
* ``finish`` -- the placeholder is swapped for one row per output, in one transaction,
  or, with no outputs (failed, cancelled), the placeholder records how it ended.

Writes go through one background thread in submission order, so a slow or unreachable
database never delays or fails a job: history is best effort, the job is not. Nothing
is recorded when the app database is not configured.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import app_db, config
from ..errors import ApiError, not_found

log = logging.getLogger("ahi.history")

CLEAN = "clean"
BRONZE = "bronze_ingest"
SILVER = "silver_load"
TYPES = (CLEAN, BRONZE, SILVER)
STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "skipped", "interrupted")

# A run shows the most pressing status among its rows.
_PRIORITY = ["running", "queued", "failed", "interrupted", "cancelled", "succeeded", "skipped"]

MAX_LOG_CHARS = 200_000


def _uuid(value) -> str | None:
    """A UUID string, or None for anything that is not one (e.g. ids from before UUIDs)."""
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _sha256(value) -> str | None:
    text = str(value or "").lower()
    return text if len(text) == 64 and all(c in "0123456789abcdef" for c in text) else None


def _ts(epoch: float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch else None


# --------------------------------------------------------------------------- #
# Logs, per run, in memory until the run ends
# --------------------------------------------------------------------------- #


@dataclass
class _Log:
    lines: list[str] = field(default_factory=list)
    last: str = ""

    def add(self, message: str, level: str = "INFO") -> None:
        if message == self.last:  # stage messages repeat while progress ticks
            return
        self.last = message
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.lines.append(f"{stamp}Z  {level:<5}  {message}")

    def text(self) -> str:
        joined = "\n".join(self.lines)
        return joined if len(joined) <= MAX_LOG_CHARS else "…\n" + joined[-MAX_LOG_CHARS:]


_logs: dict[str, _Log] = {}
_logs_lock = threading.Lock()


def note(job_id: str, message: str, level: str = "INFO") -> None:
    if not config.APP_DB_CONFIGURED:
        return
    with _logs_lock:
        _logs.setdefault(job_id, _Log()).add(message, level)


def _log_text(job_id: str, drop: bool = False) -> str | None:
    with _logs_lock:
        found = _logs.pop(job_id, None) if drop else _logs.get(job_id)
    return found.text() if found else None


# --------------------------------------------------------------------------- #
# The writer thread
# --------------------------------------------------------------------------- #

_queue: "queue.Queue[tuple]" = queue.Queue()
_writer: threading.Thread | None = None
_writer_lock = threading.Lock()


def _submit(operation, *args) -> None:
    global _writer
    if not config.APP_DB_CONFIGURED:
        return
    with _writer_lock:
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_drain, name="job-history", daemon=True)
            _writer.start()
    _queue.put((operation, args))


def _drain() -> None:
    while True:
        operation, args = _queue.get()
        try:
            with app_db.connection() as conn:
                operation(conn, *args)
        except Exception as error:  # noqa: BLE001 - history must never break a job
            log.warning("Job history write failed (%s): %s", operation.__name__, error)
        finally:
            _queue.task_done()


def flush(timeout: float = 10.0) -> bool:
    """Wait until every queued write has been attempted; for shutdown and tests."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def begin(job_type: str, job_id: str, *, created_by: str | None, status: str = "queued",
          batch_id: str | None = None, source_job_id: str | None = None, source_file: str | None = None,
          source_sha256: str | None = None, source_sheets: list[str] | None = None,
          started_at: float | None = None) -> None:
    if not config.APP_DB_CONFIGURED:
        return
    note(job_id, f"{job_type.replace('_', ' ').capitalize()} {status}")
    row = {
        "job_id": _uuid(job_id), "job_type": job_type, "status": status, "batch_id": _uuid(batch_id),
        "source_job_id": _uuid(source_job_id), "source_file": (source_file or None),
        "source_sha256": _sha256(source_sha256), "source_sheets": source_sheets or None,
        "created_by": _uuid(created_by), "started_at": _ts(started_at), "log": _log_text(job_id),
    }
    if row["job_id"] is None:
        log.warning("Job history skipped: %r is not a UUID", job_id)
        return
    _submit(_insert_placeholder, row)


def _insert_placeholder(conn, row: dict) -> None:
    from psycopg import sql

    names = list(row)
    conn.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            app_db.table("jobs"), sql.SQL(", ").join(map(sql.Identifier, names)),
            sql.SQL(", ").join(sql.Placeholder(name) for name in names)),
        row,
    )


def update(job_id: str, *, status: str | None = None, started_at: float | None = None,
           modified_by: str | None = None) -> None:
    """Change the run's placeholder row; the log so far is flushed with it."""
    if not config.APP_DB_CONFIGURED or _uuid(job_id) is None:
        return
    if status:
        note(job_id, f"Status: {status}")
    _submit(_update_placeholder, _uuid(job_id), status, _ts(started_at), _uuid(modified_by), _log_text(job_id))


def _update_placeholder(conn, job_id, status, started_at, modified_by, text) -> None:
    from psycopg import sql

    conn.execute(
        sql.SQL("UPDATE {} SET status = coalesce(%s, status), started_at = coalesce(%s, started_at), "
                "modified_by = coalesce(%s, modified_by), log = coalesce(%s, log) "
                "WHERE job_id = %s AND output_name IS NULL").format(app_db.table("jobs")),
        [status, started_at, modified_by, text, job_id],
    )


def finish(job_id: str, status: str, outputs: list[dict] | None = None, *, ended_at: float | None = None,
           modified_by: str | None = None) -> None:
    """End a run. ``outputs``: one dict per row -- output_name, output_file, row_count,
    rows_removed and optionally status, source_file, source_sha256, source_sheets, source_job_id."""
    if not config.APP_DB_CONFIGURED or _uuid(job_id) is None:
        return
    note(job_id, f"Finished: {status}" + (f", {len(outputs)} output(s)" if outputs else ""),
         "ERROR" if status == "failed" else "INFO")
    _submit(_finish, _uuid(job_id), status, outputs or [], _ts(ended_at or time.time()),
            _uuid(modified_by), _log_text(job_id, drop=True))


def _finish(conn, job_id, status, outputs, ended_at, modified_by, text) -> None:
    from psycopg import sql

    jobs = app_db.table("jobs")
    if not outputs:
        conn.execute(
            sql.SQL("UPDATE {} SET status = %s, ended_at = %s, modified_by = coalesce(%s, modified_by), "
                    "log = coalesce(%s, log) WHERE job_id = %s AND output_name IS NULL").format(jobs),
            [status, ended_at, modified_by, text, job_id],
        )
        return
    placeholder = conn.execute(
        sql.SQL("DELETE FROM {} WHERE job_id = %s AND output_name IS NULL RETURNING job_type, batch_id, "
                "source_job_id, source_file, source_sha256, source_sheets, created_at, started_at, created_by, "
                "modified_by").format(jobs), [job_id]).fetchone()
    if placeholder is None:
        log.warning("Job history: no placeholder for %s; outputs not recorded", job_id)
        return
    (job_type, batch_id, source_job_id, source_file, source_sha256, source_sheets,
     created_at, started_at, created_by, earlier_modified_by) = placeholder
    for output in outputs:
        conn.execute(
            sql.SQL(
                "INSERT INTO {} (job_id, job_type, status, batch_id, source_job_id, source_file, source_sha256, "
                "source_sheets, output_name, output_file, row_count, rows_removed, log, created_at, started_at, "
                "ended_at, created_by, modified_by) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)").format(jobs),
            [job_id, job_type, output.get("status") or status, batch_id,
             _uuid(output.get("source_job_id")) or source_job_id,
             output.get("source_file") or source_file,
             _sha256(output.get("source_sha256")) or source_sha256,
             output.get("source_sheets") or source_sheets,
             output.get("output_name") or "", output.get("output_file"), output.get("row_count"),
             output.get("rows_removed"), text, created_at, started_at, ended_at, created_by,
             modified_by or earlier_modified_by],
        )


def sweep_interrupted() -> None:
    """At start-up: runs left queued or running died with the previous process."""
    _submit(_sweep)


def _sweep(conn) -> None:
    from psycopg import sql

    conn.execute(
        sql.SQL("UPDATE {} SET status = 'interrupted', ended_at = now(), "
                "log = concat_ws(E'\\n', log, %s::text) WHERE status IN ('queued', 'running')").format(app_db.table("jobs")),
        [datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z  WARN   The API restarted before this job ended."],
    )


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

_ROW = ("j.job_row_id, j.job_id, j.job_type, j.status, j.batch_id, j.source_job_id, j.source_file, "
        "j.source_sha256, j.source_sheets, j.output_name, j.output_file, j.row_count, j.rows_removed, "
        "j.created_at, j.started_at, j.ended_at, j.created_by, cu.user_name, j.modified_by, mu.user_name")


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _group(rows, with_log: dict[str, str] | None = None) -> list[dict]:
    runs: dict[str, dict] = {}
    for (row_id, job_id, job_type, status, batch_id, source_job_id, source_file, source_sha256, source_sheets,
         output_name, output_file, row_count, rows_removed, created_at, started_at, ended_at,
         created_by, created_by_name, modified_by, modified_by_name) in rows:
        key = str(job_id)
        run = runs.get(key)
        if run is None:
            run = runs[key] = {
                "job_id": key, "job_type": job_type, "status": status,
                "batch_id": str(batch_id) if batch_id else None,
                "source_job_ids": [], "source_files": [], "source_sha256": [], "source_sheets": [],
                "created_at": _iso(created_at), "started_at": _iso(started_at), "ended_at": _iso(ended_at),
                "created_by": {"user_id": str(created_by), "user_name": created_by_name} if created_by else None,
                "modified_by": {"user_id": str(modified_by), "user_name": modified_by_name} if modified_by else None,
                "outputs": [], "_statuses": set(),
            }
        run["_statuses"].add(status)
        for name, value in (("source_job_ids", str(source_job_id) if source_job_id else None),
                            ("source_files", source_file), ("source_sha256", source_sha256)):
            if value and value not in run[name]:
                run[name].append(value)
        for sheet in source_sheets or []:
            if sheet not in run["source_sheets"]:
                run["source_sheets"].append(sheet)
        if ended_at and (run["ended_at"] is None or _iso(ended_at) > run["ended_at"]):
            run["ended_at"] = _iso(ended_at)
        if output_name is not None:
            run["outputs"].append({
                "job_row_id": str(row_id), "output_name": output_name, "output_file": output_file,
                "row_count": row_count, "rows_removed": rows_removed, "status": status,
                "source_file": source_file,
            })
    result = []
    for run in runs.values():
        statuses = run.pop("_statuses")
        run["status"] = next(s for s in _PRIORITY if s in statuses) if statuses else run["status"]
        if with_log is not None:
            run["log"] = with_log.get(run["job_id"])
        result.append(run)
    return result


def list_runs(viewer, *, job_type: str | None = None, status: str | None = None, q: str | None = None,
              user_id: str | None = None, before: str | None = None, limit: int = 30) -> dict:
    from psycopg import sql

    if job_type and job_type not in TYPES:
        raise ApiError(422, "invalid_type", "Unknown job type.", field="type")
    if status and status not in STATUSES:
        raise ApiError(422, "invalid_status", "Unknown status.", field="status")
    owner = viewer.user_id if not viewer.is_admin else _uuid(user_id)
    if user_id and owner is None:
        raise ApiError(422, "invalid_user", "Unknown user.", field="user_id")
    term = (q or "").strip()
    params: dict = {"type": job_type, "status": status, "owner": owner, "before": before,
                    "limit": max(1, min(limit, 100)), "like": f"%{term}%" if term else None,
                    "prefix": f"{term.lower()}%" if term else None}
    jobs, users = app_db.table("jobs"), app_db.table("users")
    query = sql.SQL(
        "WITH runs AS ("
        "  SELECT job_id, min(created_at) AS first_at FROM {jobs} j"
        "  WHERE (%(type)s::text IS NULL OR job_type = %(type)s)"
        "    AND (%(owner)s::uuid IS NULL OR created_by = %(owner)s::uuid)"
        "  GROUP BY job_id"
        "  HAVING (%(status)s::text IS NULL OR bool_or(status = %(status)s))"
        "     AND (%(before)s::timestamptz IS NULL OR min(created_at) < %(before)s::timestamptz)"
        "     AND (%(like)s::text IS NULL OR bool_or("
        "          job_id::text LIKE %(prefix)s OR source_job_id::text LIKE %(prefix)s"
        "          OR source_sha256 LIKE %(prefix)s"
        "          OR source_file ILIKE %(like)s OR output_file ILIKE %(like)s OR output_name ILIKE %(like)s))"
        "  ORDER BY first_at DESC LIMIT %(limit)s"
        ") "
        "SELECT " + _ROW + " FROM {jobs} j JOIN runs r USING (job_id) "
        "LEFT JOIN {users} cu ON cu.user_id = j.created_by LEFT JOIN {users} mu ON mu.user_id = j.modified_by "
        "ORDER BY r.first_at DESC, j.job_id, j.output_name NULLS FIRST"
    ).format(jobs=jobs, users=users)
    with app_db.connection() as conn:
        rows = conn.execute(query, params).fetchall()
    runs = _group(rows)
    runs.sort(key=lambda run: run["created_at"] or "", reverse=True)
    next_before = runs[-1]["created_at"] if len(runs) == params["limit"] else None
    return {"jobs": runs, "next_before": next_before}


def run_detail(viewer, job_id: str) -> dict:
    from psycopg import sql

    key = _uuid(job_id)
    if key is None:
        raise not_found("That job")
    jobs, users = app_db.table("jobs"), app_db.table("users")
    with app_db.connection() as conn:
        rows = conn.execute(sql.SQL(
            "SELECT " + _ROW + ", j.log FROM {jobs} j LEFT JOIN {users} cu ON cu.user_id = j.created_by "
            "LEFT JOIN {users} mu ON mu.user_id = j.modified_by WHERE j.job_id = %s "
            "ORDER BY j.output_name NULLS FIRST").format(jobs=jobs, users=users), [key]).fetchall()
    if not rows:
        raise not_found("That job")
    if not viewer.is_admin and any(str(row[16]) != viewer.user_id for row in rows):
        raise not_found("That job")
    text = next((row[-1] for row in rows if row[-1]), None)
    return _group([row[:-1] for row in rows], {key: text})[0]
