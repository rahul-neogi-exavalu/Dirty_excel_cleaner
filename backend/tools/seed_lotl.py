"""Load a LOTL CSV (pc_id, legacy_office_name, profit_center_number) into the LOTL table.

    python backend/tools/seed_lotl.py enhancements/test-files/lotl_seed.csv

Uses the database in backend/.env (or .env). The table is AHI_LOTL_TABLE, by default the
placeholder <control schema>.lotl. Rows are upserted by pc_id, so rerunning is safe.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from api import config, db  # noqa: E402
from psycopg import sql  # noqa: E402


def seed(path: Path) -> int:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = [(r["pc_id"].strip(), r["legacy_office_name"].strip(), r["profit_center_number"].strip())
                for r in csv.DictReader(handle) if r.get("pc_id")]
    schema, _, table = config.LOTL_TABLE.rpartition(".")
    target = sql.SQL("{}.{}").format(sql.Identifier(schema or config.CONTROL_SCHEMA), sql.Identifier(table))
    with db.connection() as conn:
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} (pc_id text PRIMARY KEY, legacy_office_name text NOT NULL, "
            "profit_center_number text NOT NULL)").format(target))
        for row in rows:
            conn.execute(sql.SQL(
                "INSERT INTO {} (pc_id, legacy_office_name, profit_center_number) VALUES (%s, %s, %s) "
                "ON CONFLICT (pc_id) DO UPDATE SET legacy_office_name = EXCLUDED.legacy_office_name, "
                "profit_center_number = EXCLUDED.profit_center_number").format(target), row)
    return len(rows)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    try:
        print(f"{seed(Path(sys.argv[1]))} LOTL rows loaded into {config.LOTL_TABLE}")
    finally:
        db.close()
