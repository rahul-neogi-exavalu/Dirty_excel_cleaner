"""Migration 005 and the reference seeding on a database that already holds an older
DRT column mapping: app-made ignore rows go, labels the business never uses are cleared,
the business's own rows are untouched.

Skipped unless AHI_TEST_DATABASE_URL points at a scratch database. Uses its own throwaway
schemas and drops them afterwards.
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


@pytest.fixture()
def env():
    from psycopg.conninfo import conninfo_to_dict

    from api import config, db

    parts = conninfo_to_dict(URL)
    suffix = uuid.uuid4().hex[:8]
    names = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE", "BRONZE_SCHEMA",
             "CONTROL_SCHEMA", "SILVER_SCHEMA", "CLEANSED_SCHEMA", "LOTL_TABLE", "DB_CONFIGURED", "INGEST_ENABLED")
    saved = {name: getattr(config, name) for name in names}
    config.DB_HOST, config.DB_PORT = parts.get("host", "localhost"), int(parts.get("port", 5432))
    config.DB_NAME, config.DB_USER = parts.get("dbname", "postgres"), parts.get("user", "postgres")
    config.DB_PASSWORD, config.DB_SSLMODE = parts.get("password", ""), parts.get("sslmode", "disable")
    config.BRONZE_SCHEMA, config.CONTROL_SCHEMA = f"bronze_m{suffix}", f"ingest_m{suffix}"
    config.SILVER_SCHEMA, config.CLEANSED_SCHEMA = f"silver_m{suffix}", f"cleansed_m{suffix}"
    config.LOTL_TABLE = ""
    config.DB_CONFIGURED = config.INGEST_ENABLED = True
    db.close()
    yield config, db
    db.close()
    with psycopg.connect(URL, autocommit=True) as conn:
        for schema in (config.BRONZE_SCHEMA, config.CONTROL_SCHEMA, config.SILVER_SCHEMA, config.CLEANSED_SCHEMA):
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    for name, value in saved.items():
        setattr(config, name, value)


def test_an_older_mapping_is_cleaned_once(env):
    config, db = env
    silver = config.SILVER_SCHEMA
    # The table as the earlier app left it: a business row, an app-made ignore, a label
    # the app invented, and a twin that would collide once that label is cleared.
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{silver}"')
        conn.execute(f'CREATE TABLE "{silver}".drt_column_mapping (profit_center text NOT NULL, '
                     'pc_column text NOT NULL, drt_column text, silver_column_name text)')
        conn.execute(f'INSERT INTO "{silver}".drt_column_mapping VALUES '
                     "('PC0515', 'Wr Prem', 'Premium', 'premium'), "
                     "('PC0515', 'notes', NULL, NULL), "
                     "('PC0515', 'producerofficezipcode', 'Producer Office Zipcode', 'producer_office_zipcode'), "
                     "('PC0515', 'isMga', 'Is MGA', 'is_mga'), ('PC0515', 'isMga', NULL, 'is_mga')")

    db.pool()  # migrates and seeds

    with psycopg.connect(URL) as conn:
        rows = sorted(conn.execute(f'SELECT profit_center, pc_column, drt_column, silver_column_name '
                                   f'FROM "{silver}".drt_column_mapping').fetchall(), key=str)
        labels = conn.execute(f'SELECT count(*) FROM "{config.CONTROL_SCHEMA}".drt_label').fetchone()[0]
        schemas = {row[0] for row in conn.execute("SELECT schema_name FROM information_schema.schemata")}
    # The app-made ignore goes. Since the business's 48 DRT columns (008), "Is MGA" and
    # "Producer Office Zipcode" are its own labels: they stay, and isMga's twin is one row.
    assert rows == sorted([
        ("PC0515", "Wr Prem", "Premium", "premium"),
        ("PC0515", "producerofficezipcode", "Producer Office Zipcode", "producer_office_zipcode"),
        ("PC0515", "isMga", "Is MGA", "is_mga"),
    ], key=str)
    assert labels == 48  # every DRT column of the Silver catalog
    assert config.CLEANSED_SCHEMA in schemas
