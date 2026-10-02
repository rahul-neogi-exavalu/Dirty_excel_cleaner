"""The app database: users and job history against a real Postgres.

Skipped unless AHI_TEST_DATABASE_URL points at a database the tests may create schemas in.
Each run uses a throwaway schema and drops it afterwards.
"""

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

URL = os.environ.get("AHI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="AHI_TEST_DATABASE_URL is not set")

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def app_db_env(signed_in, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict

    from api import app_db, config

    parts = conninfo_to_dict(URL)
    schema = f"app_t{uuid.uuid4().hex[:8]}"
    for name, value in {
        "APP_DB_HOST": parts.get("host", "localhost"), "APP_DB_PORT": int(parts.get("port", 5432)),
        "APP_DB_NAME": parts.get("dbname", "postgres"), "APP_DB_USER": parts.get("user", "postgres"),
        "APP_DB_PASSWORD": parts.get("password", ""), "APP_DB_SSLMODE": "disable",
        "APP_DB_SCHEMA": schema, "APP_DB_CONFIGURED": True,
    }.items():
        monkeypatch.setattr(config, name, value)
    app_db.close()
    yield app_db, schema
    app_db.close()
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _add_user(app_db, email: str) -> str:
    from psycopg import sql

    from api import auth

    with app_db.connection() as conn:
        row = conn.execute(sql.SQL("INSERT INTO {} (user_name, user_email_id, password_hash) VALUES (%s, %s, %s) "
                                   "RETURNING user_id").format(app_db.table("users")),
                           ["Ada", email, auth.hash_password("x" * 12, 1000)]).fetchone()
    return str(row[0])


def test_emails_are_unique_whatever_their_case(app_db_env):
    app_db, _ = app_db_env
    _add_user(app_db, "ada@example.com")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _add_user(app_db, "ADA@Example.com")


def test_modified_at_moves_on_update_but_not_on_sign_in(app_db_env):
    from psycopg import sql

    app_db, _ = app_db_env
    user_id = _add_user(app_db, "bob@example.com")
    users = app_db.table("users")
    with app_db.connection() as conn:
        before = conn.execute(sql.SQL("SELECT modified_at FROM {} WHERE user_id = %s").format(users), [user_id]).fetchone()[0]
    with app_db.connection() as conn:
        conn.execute(sql.SQL("UPDATE {} SET last_login_at = now() WHERE user_id = %s").format(users), [user_id])
    with app_db.connection() as conn:
        after_login = conn.execute(sql.SQL("SELECT modified_at FROM {} WHERE user_id = %s").format(users), [user_id]).fetchone()[0]
        conn.execute(sql.SQL("UPDATE {} SET user_name = 'Bobby' WHERE user_id = %s").format(users), [user_id])
    with app_db.connection() as conn:
        after_edit = conn.execute(sql.SQL("SELECT modified_at FROM {} WHERE user_id = %s").format(users), [user_id]).fetchone()[0]
    assert after_login == before
    assert after_edit > before


def test_a_run_becomes_one_row_per_output_sharing_its_job_id(app_db_env):
    from api.services import job_history

    app_db, _ = app_db_env
    owner = _add_user(app_db, "cy@example.com")
    job_id = str(uuid.uuid4())
    job_history.begin(job_history.CLEAN, job_id, created_by=owner, source_file="book.xlsx",
                      source_sha256="a" * 64, source_sheets=["Jan", "Feb", "Mar"])
    job_history.update(job_id, status="running")
    job_history.note(job_id, "Cleaning")
    job_history.finish(job_id, "succeeded", [
        {"output_name": f"book_{sheet}", "output_file": f"book_{sheet}_{job_id}.csv", "row_count": 10}
        for sheet in ("Jan", "Feb", "Mar")
    ])
    assert job_history.flush()

    class Viewer:
        user_id, is_admin = owner, False

    detail = job_history.run_detail(Viewer, job_id)
    assert detail["status"] == "succeeded"
    assert [output["output_file"] for output in detail["outputs"]] == [
        f"book_{sheet}_{job_id}.csv" for sheet in ("Feb", "Jan", "Mar")]
    assert "Cleaning" in detail["log"]
    listed = job_history.list_runs(Viewer, q="a" * 64)
    assert [run["job_id"] for run in listed["jobs"]] == [job_id]


def test_runs_left_running_are_marked_interrupted(app_db_env):
    from api.services import job_history

    app_db, _ = app_db_env
    job_id = str(uuid.uuid4())
    job_history.begin(job_history.CLEAN, job_id, created_by=None, status="running")
    job_history.sweep_interrupted()
    assert job_history.flush()

    class Admin:
        user_id, is_admin = None, True

    assert job_history.run_detail(Admin, job_id)["status"] == "interrupted"
