"""The bronze database: a small connection pool and the control-table migrations.

Nothing connects until the Ingest step is used, so the cleaning service runs exactly as
before when no database is configured.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

from . import config
from .errors import ApiError

MIGRATIONS = config.ROOT / "migrations"

_lock = threading.Lock()
_pool = None
_migrated = False


def require() -> None:
    if not config.INGEST_ENABLED:
        raise ApiError(404, "ingest_disabled", "Ingestion is turned off on this server.")
    if not config.DB_CONFIGURED:
        raise ApiError(
            503, "db_not_configured", "The bronze database is not configured.",
            "Set AHI_DB_HOST, AHI_DB_NAME, AHI_DB_USER and AHI_DB_PASSWORD in .env, then restart the API.",
        )


def pool():
    """The shared pool, opened and migrated on first use."""
    global _pool, _migrated
    require()
    with _lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool

            _pool = ConnectionPool(config.db_conninfo(), min_size=1, max_size=4, open=False,
                                   name="bronze", timeout=15)
            try:
                _pool.open(wait=True, timeout=15)
            except Exception as error:
                _pool.close()
                _pool = None
                raise unreachable(error) from error
        if not _migrated:
            try:
                migrate(_pool)
            except Exception as error:
                if _is_connection_error(error):
                    raise unreachable(error) from error
                raise ApiError(
                    503, "db_setup_failed", "The bronze database could not be prepared.",
                    "Check that the database user may create schemas and tables.",
                    detail=str(error).strip()[:500],
                ) from error
            _migrated = True
    return _pool


@contextmanager
def connection():
    """A pooled connection (one transaction); lost connections become a clear 503."""
    try:
        with pool().connection() as conn:
            yield conn
    except ApiError:
        raise
    except Exception as error:
        if _is_connection_error(error):
            raise unreachable(error) from error
        raise


def _is_connection_error(error: Exception) -> bool:
    import psycopg
    from psycopg_pool import PoolTimeout

    return isinstance(error, (psycopg.OperationalError, PoolTimeout))


def unreachable(error: Exception) -> ApiError:
    return ApiError(
        503, "db_unreachable", "The bronze database could not be reached.",
        "Check the connection settings in .env and that the database accepts connections from this machine.",
        detail=str(error).strip()[:500],
    )


def migrate(target_pool) -> list[str]:
    """Apply the numbered SQL files in ``backend/migrations`` that have not run yet."""
    from psycopg import sql

    control = sql.Identifier(config.CONTROL_SCHEMA)
    applied = []
    with target_pool.connection() as conn:
        # One migrator at a time, even across API processes.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('ahi-migrations'))")
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(control))
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        ).format(control))
        done = {row[0] for row in conn.execute(sql.SQL("SELECT name FROM {}.schema_migrations").format(control))}
        for path in sorted(Path(MIGRATIONS).glob("*.sql")):
            if path.name in done:
                continue
            text = path.read_text(encoding="utf-8").replace("{control}", control.as_string(conn))
            conn.execute(text)
            conn.execute(sql.SQL("INSERT INTO {}.schema_migrations (name) VALUES (%s)").format(control), [path.name])
            applied.append(path.name)
    return applied


def status() -> dict:
    """What the UI needs to decide whether the Ingest step is usable."""
    info = {
        "enabled": config.INGEST_ENABLED,
        "configured": config.DB_CONFIGURED,
        "reachable": False,
        "host": config.DB_HOST or None,
        "database": config.DB_NAME or None,
        "bronze_schema": config.BRONZE_SCHEMA,
        "error": None,
    }
    if not (config.INGEST_ENABLED and config.DB_CONFIGURED):
        return info
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        info["reachable"] = True
    except ApiError as error:
        info["error"] = error.message + (f" ({error.detail})" if error.detail else "")
    except Exception as error:  # noqa: BLE001 - a health check reports, never raises
        info["error"] = str(error).strip()[:300]
    return info


def close() -> None:
    global _pool, _migrated
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool, _migrated = None, False
