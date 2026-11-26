"""The app database: users and job history, in their own pool and migrations.

Separate from the bronze database on purpose. Sign-in must keep working when ingestion
is switched off, and the two may live in different databases (``APP_DB_*`` vs ``AHI_DB_*``).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from . import config, db
from .errors import ApiError

MIGRATIONS = config.ROOT / "migrations_app"

_lock = threading.Lock()
_pool = None
_migrated = False


def require() -> None:
    if not config.APP_DB_CONFIGURED:
        raise ApiError(
            503, "app_db_not_configured", "Sign-in is not set up on this server.",
            "Set APP_DB_HOST, APP_DB_NAME, APP_DB_USER and APP_DB_PASSWORD in .env, then restart the API.",
        )


def pool():
    """The shared pool, opened and migrated on first use."""
    global _pool, _migrated
    require()
    with _lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool

            _pool = ConnectionPool(config.app_db_conninfo(), min_size=1, max_size=4, open=False,
                                   name="app", timeout=15)
            try:
                _pool.open(wait=True, timeout=15)
            except Exception as error:
                _pool.close()
                _pool = None
                raise unreachable(error) from error
        if not _migrated:
            try:
                db.run_migrations(_pool, MIGRATIONS, config.APP_DB_SCHEMA, "{app}", "ahi-app-migrations")
            except Exception as error:
                if db._is_connection_error(error):
                    raise unreachable(error) from error
                raise ApiError(
                    503, "app_db_setup_failed", "The app database could not be prepared.",
                    "Check that the database user may create tables in APP_DB_SCHEMA.",
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
        if db._is_connection_error(error):
            raise unreachable(error) from error
        raise


def unreachable(error: Exception) -> ApiError:
    return ApiError(
        503, "app_db_unreachable", "Sign-in is unavailable right now.",
        "The app database could not be reached. Try again shortly.",
        detail=str(error).strip()[:500],
    )


def table(name: str):
    """``schema.name`` as a safely quoted identifier."""
    from psycopg import sql

    return sql.Identifier(config.APP_DB_SCHEMA, name)


def close() -> None:
    global _pool, _migrated
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool, _migrated = None, False
