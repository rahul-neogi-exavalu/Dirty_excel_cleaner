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
                # The business's reference tables, filled from assets/ while they are empty.
                from .services import bronze_service, reference_service, silver_service

                with _pool.connection() as conn:
                    reference_service.seed_control(conn)
                    reference_service.seed_silver(conn, silver_service.catalog())
                    # Every DRT column of the Silver schema, and the mapping rows' DRT columns.
                    reference_service.sync_drt_columns(conn, silver_service.catalog())
                    # After the DRT mapping, which seeds the bronze mapping.
                    reference_service.seed_bronze_mapping(conn)
                    reference_service.seed_control_table(conn)
                    # Bronze tables made before file_received_date and the reporting dates.
                    bronze_service.upgrade_tables(conn)
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


def prepared() -> bool:
    """Whether the pool is open and the database migrated and seeded (the first use does both)."""
    return _pool is not None and _migrated


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
    return run_migrations(target_pool, MIGRATIONS, config.CONTROL_SCHEMA, "{control}", "ahi-migrations",
                          extra={"{silver}": config.SILVER_SCHEMA, "{cleansed}": config.CLEANSED_SCHEMA,
                                 "{bronze}": config.BRONZE_SCHEMA, "{staging}": config.STAGING_SCHEMA})


def run_migrations(target_pool, folder: Path, schema: str, placeholder: str, lock_key: str,
                   extra: dict[str, str] | None = None) -> list[str]:
    """Apply the SQL files in ``folder`` not yet recorded in ``schema.schema_migrations``.

    ``placeholder`` in a file is replaced by the quoted schema name, and each of ``extra``'s
    placeholders by its quoted schema. Files run in name order, each in the one
    transaction, under an advisory lock so two API processes starting together never
    migrate twice.
    """
    from psycopg import sql

    target = sql.Identifier(schema)
    applied = []
    with target_pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [lock_key])
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(target))
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        ).format(target))
        done = {row[0] for row in conn.execute(sql.SQL("SELECT name FROM {}.schema_migrations").format(target))}
        names = {placeholder: target.as_string(conn)}
        names.update({key: sql.Identifier(value).as_string(conn) for key, value in (extra or {}).items()})
        for path in sorted(Path(folder).glob("*.sql")):
            if path.name in done:
                continue
            text = path.read_text(encoding="utf-8")
            for key, quoted in names.items():
                text = text.replace(key, quoted)
            conn.execute(text)
            conn.execute(sql.SQL("INSERT INTO {}.schema_migrations (name) VALUES (%s)").format(target), [path.name])
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
