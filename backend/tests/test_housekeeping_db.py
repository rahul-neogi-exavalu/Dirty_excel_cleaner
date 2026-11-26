"""tools/db_housekeeping.py on a database out of line with its settings: a cleansed table
in a stray schema, a bronze registration whose table is gone, and the retired Silver
column_mapping.

Skipped unless AHI_TEST_DATABASE_URL points at a scratch database. Uses its own throwaway
schemas and drops them afterwards.
"""

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

URL = os.environ.get("AHI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="AHI_TEST_DATABASE_URL is not set")

psycopg = pytest.importorskip("psycopg")


@pytest.fixture()
def env(tmp_path):
    from psycopg.conninfo import conninfo_to_dict

    from api import config, db

    parts = conninfo_to_dict(URL)
    suffix = uuid.uuid4().hex[:8]
    names = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE", "BRONZE_SCHEMA",
             "CONTROL_SCHEMA", "SILVER_SCHEMA", "CLEANSED_SCHEMA", "STAGING_SCHEMA", "LOTL_TABLE", "DB_CONFIGURED",
             "INGEST_ENABLED")
    saved = {name: getattr(config, name) for name in names}
    config.DB_HOST, config.DB_PORT = parts.get("host", "localhost"), int(parts.get("port", 5432))
    config.DB_NAME, config.DB_USER = parts.get("dbname", "postgres"), parts.get("user", "postgres")
    config.DB_PASSWORD, config.DB_SSLMODE = parts.get("password", ""), parts.get("sslmode", "disable")
    config.BRONZE_SCHEMA, config.CONTROL_SCHEMA = f"bronze_h{suffix}", f"ingest_h{suffix}"
    config.SILVER_SCHEMA, config.CLEANSED_SCHEMA = f"silver_h{suffix}", f"cleansed_h{suffix}"
    config.STAGING_SCHEMA, config.LOTL_TABLE = f"staging_h{suffix}", ""
    config.DB_CONFIGURED = config.INGEST_ENABLED = True
    stray = f"stray_h{suffix}"
    db.close()
    yield config, db, stray, tmp_path
    db.close()
    with psycopg.connect(URL, autocommit=True) as conn:
        for schema in (config.BRONZE_SCHEMA, config.CONTROL_SCHEMA, config.SILVER_SCHEMA, config.CLEANSED_SCHEMA,
                       config.STAGING_SCHEMA, stray):
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    for name, value in saved.items():
        setattr(config, name, value)


def test_housekeeping_puts_the_database_back_in_line(env):
    import db_housekeeping as housekeeping

    config, db, stray, backups = env
    control, bronze, silver, cleansed = (config.CONTROL_SCHEMA, config.BRONZE_SCHEMA, config.SILVER_SCHEMA,
                                         config.CLEANSED_SCHEMA)
    db.pool()  # migrates and seeds
    with psycopg.connect(URL, autocommit=True) as conn:
        # A cleansed table a server with another setting made, in a schema of its own.
        conn.execute(f'CREATE SCHEMA "{stray}"')
        conn.execute(f'CREATE TABLE "{stray}".ext_pc0101_arr (premium numeric, _ingestion_id text NOT NULL)')
        conn.execute(f'CREATE INDEX ext_pc0101_arr_load ON "{stray}".ext_pc0101_arr (_ingestion_id)')
        conn.execute(f"INSERT INTO \"{stray}\".ext_pc0101_arr VALUES (10, 'live-0101')")
        conn.execute(f'INSERT INTO "{control}".silver_cleansed_table (bronze_table, cleansed_table) VALUES (%s, %s)',
                     ["ext_pc0101_arr", f"{stray}.ext_pc0101_arr"])
        # Two registered bronze tables: one there, one gone (with a live and a skipped load).
        conn.execute(f'CREATE TABLE "{bronze}".ext_pc0101_arr (premium text)')
        for name in ("ext_pc0101_arr", "ext_pc0202_data"):
            conn.execute(f'INSERT INTO "{control}".bronze_table (table_name, schema_name, source_system, columns) '
                         "VALUES (%s, %s, 'pc0101', '[]')", [name, bronze])
        conn.execute(f'INSERT INTO "{control}".ingestion (id, table_name, file_name, file_sha256, action, status, '
                     "silver_status) VALUES "
                     "('live-0101', 'ext_pc0101_arr', 'a.xlsx', 'h1', 'create', 'ingested', 'succeeded'), "
                     "('live-0202', 'ext_pc0202_data', 'b.xlsx', 'h2', 'create', 'ingested', 'succeeded'), "
                     "('skip-0202', 'ext_pc0202_data', 'c.xlsx', 'h3', 'skip', 'skipped', NULL)")
        # The mapping table drt_column_mapping replaced.
        conn.execute(f'CREATE TABLE "{silver}".column_mapping (profit_center text, bronze_table_name text, '
                     "bronze_column_name text, drt_column_name text, silver_column_name text, "
                     "PRIMARY KEY (profit_center, bronze_table_name, bronze_column_name))")
        conn.execute(f'INSERT INTO "{silver}".column_mapping VALUES '
                     "('PC0101', 'ext_pc0101_arr', 'wr_prem', 'Premium', 'premium'), "
                     "('PC0101', 'ext_pc0101_arr', 'agent', 'Producer Name', 'producer_agency_name')")

    with db.connection() as conn:
        found = housekeeping.survey(conn)
    assert found.cleansed == [("ext_pc0101_arr", stray, "ext_pc0101_arr")]
    assert found.orphans == {"ext_pc0202_data": 1}
    assert [(schema, table, rows) for schema, table, _, rows in found.retired] == [(silver, "column_mapping", 2)]

    with db.connection() as conn:
        done = housekeeping.apply(conn, found, backups)
    assert f"dropped schema {stray} (empty)" in done

    with psycopg.connect(URL) as conn:
        def one(query, params=None):
            return conn.execute(query, params).fetchall()

        assert one(f'SELECT premium, _ingestion_id FROM "{cleansed}".ext_pc0101_arr') == [(10, "live-0101")]
        assert one("SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = 'ext_pc0101_arr'",
                   [cleansed]) == [("ext_pc0101_arr_load",)]
        assert one(f'SELECT cleansed_table FROM "{control}".silver_cleansed_table') == [(f"{cleansed}.ext_pc0101_arr",)]
        assert not one("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [stray])
        assert one(f'SELECT table_name FROM "{control}".bronze_table') == [("ext_pc0101_arr",)]
        assert dict((row[0], row[1:]) for row in one(f'SELECT id, status, silver_status FROM "{control}".ingestion')) == {
            "live-0101": ("ingested", "succeeded"),  # its table is there: untouched
            "live-0202": ("removed", "removed"),
            "skip-0202": ("skipped", None),
        }
        assert one("SELECT to_regclass(%s)", [f'"{silver}".column_mapping']) == [(None,)]
    # Backed up first: every row, and the SQL to recreate the table with its key.
    lines = next(backups.glob(f"{silver}.column_mapping.*.csv")).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "profit_center,bronze_table_name,bronze_column_name,drt_column_name,silver_column_name"
    assert len(lines) == 3
    ddl = next(backups.glob(f"{silver}.column_mapping.*.sql")).read_text(encoding="utf-8")
    assert f'CREATE TABLE "{silver}"."column_mapping"' in ddl and "CREATE UNIQUE INDEX column_mapping_pkey" in ddl

    with db.connection() as conn:
        assert housekeeping.survey(conn).empty
