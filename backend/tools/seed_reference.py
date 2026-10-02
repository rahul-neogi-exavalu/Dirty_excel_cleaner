"""Load the business's reference tables from the workbooks in assets/.

    python backend/tools/seed_reference.py                  # fill empty tables only
    python backend/tools/seed_reference.py --replace        # reload division_mapping and the LOTL
    python backend/tools/seed_reference.py --replace-drt    # also reload drt_column_mapping (!)
    python backend/tools/seed_reference.py --add-lotl extra.csv   # add rows to the LOTL

Uses the database in backend/.env (or .env). The tables are filled automatically the first
time the API uses the database, so this is only needed to reload them after the business
sends new workbooks, or to add LOTL rows (a test fixture, a new office).

--replace-drt deletes every row of drt_column_mapping, including the mappings reviewers
approved in Silver runs (they are saved in the same table), and reloads the workbook.

--add-lotl takes a CSV with the LOTL's columns: profit_center_number, legacy_office_name,
status.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from api import config, db  # noqa: E402
from api.errors import ApiError  # noqa: E402
from api.services import reference_service, silver_service  # noqa: E402


def add_lotl(path: Path) -> int:
    """Append LOTL rows from a CSV (profit_center_number, legacy_office_name, status)."""
    from psycopg import sql

    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = [(r.get("profit_center_number") or None, r.get("legacy_office_name") or None, r.get("status") or None)
                for r in csv.DictReader(handle)]
    schema, table = reference_service.lotl_name().split(".", 1)
    with db.connection() as conn:
        for row in rows:
            conn.execute(sql.SQL("INSERT INTO {}.{} (profit_center_number, legacy_office_name, status) "
                                 "VALUES (%s, %s, %s)").format(sql.Identifier(schema), sql.Identifier(table)), row)
    return len(rows)


def seed(replace: bool = False, replace_drt: bool = False) -> dict[str, int]:
    with db.connection() as conn:
        loaded = reference_service.seed_control(conn, replace=replace)
        count = reference_service.ensure_drt_table(conn, silver_service.catalog(), replace=replace_drt)
        if count:
            loaded[f"{config.SILVER_SCHEMA}.{reference_service.DRT_TABLE}"] = count
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replace", action="store_true", help="reload division_mapping and the LOTL")
    parser.add_argument("--replace-drt", action="store_true",
                        help="reload drt_column_mapping, discarding approved mappings")
    parser.add_argument("--add-lotl", type=Path, help="CSV of LOTL rows to append")
    args = parser.parse_args()
    try:
        loaded = seed(args.replace, args.replace_drt)
        for table, count in loaded.items():
            print(f"{count} rows loaded into {table}")
        if not loaded:
            print(f"Nothing loaded: the tables already have rows (reference workbooks: {config.REFERENCE_DIR}).")
        if args.add_lotl:
            print(f"{add_lotl(args.add_lotl)} rows added to {reference_service.lotl_name()}")
        return 0
    except ApiError as error:
        print(f"{error.message} {error.advice or ''}".strip(), file=sys.stderr)
        if error.detail:
            print(error.detail, file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
