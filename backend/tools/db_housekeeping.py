"""Bring the database back in line with the schemas the app is configured to use.

    python backend/tools/db_housekeeping.py           # show what would change
    python backend/tools/db_housekeeping.py --apply   # change it, in one transaction

Uses the database in backend/.env (or .env). What it looks for:

* Silver Cleansed tables registered outside the cleansed schema (AHI_CLEANSED_SCHEMA),
  left there by a server that ran with another setting: moved into it, the registry
  updated, and a schema they leave empty dropped.
* Bronze tables in the registry that exist neither in the bronze schema (AHI_BRONZE_SCHEMA)
  nor in the schema the registry names: their registry rows are deleted and their live
  loads marked ``removed`` in the ingestion audit, so Ingest no longer plans against
  tables that are gone and Silver no longer counts them as loaded.
* Tables the app no longer uses: written to backend/.workspace/backups (CSV, plus the SQL
  to recreate the table) and then dropped.

It takes the Bronze and Silver locks, so it never overlaps a Validate, an Ingest or a
Silver load, and stops if one is running.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from api import config, db  # noqa: E402
from api.errors import ApiError  # noqa: E402

# (config attribute of the schema, table, why it is no longer used)
RETIRED = (("SILVER_SCHEMA", "column_mapping", "replaced by drt_column_mapping"),)
# What a load gone with its bronze table is marked in the ingestion audit.
REMOVED = "removed"


@dataclass
class Survey:
    # (bronze table, schema the cleansed table is in, cleansed table)
    cleansed: list[tuple[str, str, str]] = field(default_factory=list)
    # bronze table -> live loads (ingestion rows with status 'ingested')
    orphans: dict[str, int] = field(default_factory=dict)
    # (schema, table, reason, rows)
    retired: list[tuple[str, str, str, int]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.cleansed or self.orphans or self.retired)


def _ident(name: str):
    from psycopg import sql

    return sql.Identifier(name)


def _exists(conn, schema: str, table: str) -> bool:
    return conn.execute("SELECT to_regclass(format('%%I.%%I', %s::text, %s::text)) IS NOT NULL",
                        [schema, table]).fetchone()[0]


def survey(conn) -> Survey:
    from psycopg import sql

    found = Survey()
    control = _ident(config.CONTROL_SCHEMA)
    if _exists(conn, config.CONTROL_SCHEMA, "silver_cleansed_table"):
        for bronze, qualified in conn.execute(sql.SQL(
                "SELECT bronze_table, cleansed_table FROM {}.silver_cleansed_table ORDER BY 1").format(control)):
            schema, _, table = qualified.rpartition(".")
            if schema != config.CLEANSED_SCHEMA:
                found.cleansed.append((bronze, schema, table))
    if _exists(conn, config.CONTROL_SCHEMA, "bronze_table"):
        for name, live in conn.execute(sql.SQL(
                "SELECT b.table_name, (SELECT count(*) FROM {c}.ingestion i "
                "                      WHERE i.table_name = b.table_name AND i.status = 'ingested') "
                "FROM {c}.bronze_table b "
                "WHERE to_regclass(format('%%I.%%I', %s::text, b.table_name)) IS NULL "
                "  AND to_regclass(format('%%I.%%I', b.schema_name, b.table_name)) IS NULL "
                "ORDER BY 1").format(c=control), [config.BRONZE_SCHEMA]):
            found.orphans[name] = live
    for attribute, table, reason in RETIRED:
        schema = getattr(config, attribute)
        if _exists(conn, schema, table):
            rows = conn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(_ident(schema), _ident(table))).fetchone()[0]
            found.retired.append((schema, table, reason, rows))
    return found


def _move_cleansed(conn, found: Survey) -> list[str]:
    import psycopg
    from psycopg import sql

    done, left = [], set()
    target = config.CLEANSED_SCHEMA
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(_ident(target)))
    for bronze, schema, table in found.cleansed:
        here, there = _exists(conn, schema, table), _exists(conn, target, table)
        if here and there:
            done.append(f"SKIPPED {schema}.{table}: {target}.{table} exists too; merge them by hand.")
            continue
        if here:
            conn.execute(sql.SQL("ALTER TABLE {}.{} SET SCHEMA {}").format(_ident(schema), _ident(table), _ident(target)))
            done.append(f"moved {schema}.{table} -> {target}.{table}")
        else:
            done.append(f"{schema}.{table} is gone; the registry now names {target}.{table}")
        conn.execute(sql.SQL("UPDATE {}.silver_cleansed_table SET cleansed_table = %s, updated_at = now() "
                             "WHERE bronze_table = %s").format(_ident(config.CONTROL_SCHEMA)),
                     [f"{target}.{table}", bronze])
        left.add(schema)
    configured = {config.BRONZE_SCHEMA, config.CONTROL_SCHEMA, config.SILVER_SCHEMA, config.CLEANSED_SCHEMA,
                  config.STAGING_SCHEMA}
    for schema in sorted(left - configured):
        try:
            with conn.transaction():  # a savepoint: a schema that still holds something stays
                conn.execute(sql.SQL("DROP SCHEMA {}").format(_ident(schema)))
            done.append(f"dropped schema {schema} (empty)")
        except psycopg.errors.DependentObjectsStillExist:
            done.append(f"kept schema {schema}: it still holds other objects")
    return done


def _retire_orphans(conn, found: Survey) -> list[str]:
    from psycopg import sql

    if not found.orphans:
        return []
    names = list(found.orphans)
    control = _ident(config.CONTROL_SCHEMA)
    # Silver rows that came from these tables stay in Silver; the loads say so.
    in_silver: set[str] = set()
    if _exists(conn, config.SILVER_SCHEMA, "silver_detail"):
        in_silver = {row[0].rpartition(".")[2] for row in conn.execute(sql.SQL(
            "SELECT DISTINCT source_table FROM {}.silver_detail WHERE split_part(source_table, '.', 2) = ANY(%s)").format(
                _ident(config.SILVER_SCHEMA)), [names])}
    marked = conn.execute(sql.SQL(
        "UPDATE {}.ingestion SET status = %s, "
        "silver_status = CASE WHEN silver_status = 'succeeded' AND NOT table_name = ANY(%s) THEN %s ELSE silver_status END "
        "WHERE table_name = ANY(%s) AND status = 'ingested'").format(control),
        [REMOVED, list(in_silver), REMOVED, names]).rowcount
    deleted = conn.execute(sql.SQL("DELETE FROM {}.bronze_table WHERE table_name = ANY(%s)").format(control),
                           [names]).rowcount
    done = [f"removed {deleted} bronze registrations with no table: {', '.join(names)}",
            f"marked {marked} of their loads '{REMOVED}' in {config.CONTROL_SCHEMA}.ingestion"]
    if in_silver:
        done.append(f"Silver still holds rows from {', '.join(sorted(in_silver))}; left in place")
    return done


def _backup(conn, schema: str, table: str, folder: Path) -> Path:
    from psycopg import sql

    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{schema}.{table}.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    columns = conn.execute(
        "SELECT column_name, format_type(a.atttypid, a.atttypmod), a.attnotnull "
        "FROM information_schema.columns c "
        "JOIN pg_attribute a ON a.attrelid = format('%%I.%%I', c.table_schema, c.table_name)::regclass "
        "                   AND a.attname = c.column_name "
        "WHERE c.table_schema = %s AND c.table_name = %s ORDER BY c.ordinal_position", [schema, table]).fetchall()
    indexes = [row[0] for row in conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s ORDER BY indexname", [schema, table])]
    body = ",\n".join(f'    "{name}" {kind}{" NOT NULL" if not_null else ""}' for name, kind, not_null in columns)
    (folder / f"{stem}.sql").write_text(
        f'-- Recreate, then: \\copy "{schema}"."{table}" FROM \'{stem}.csv\' WITH (FORMAT csv, HEADER)\n'
        f'CREATE TABLE "{schema}"."{table}" (\n{body}\n);\n' + "".join(f"{index};\n" for index in indexes),
        encoding="utf-8")
    path = folder / f"{stem}.csv"
    with conn.cursor() as cursor, open(path, "wb") as handle:
        with cursor.copy(sql.SQL("COPY {}.{} TO STDOUT WITH (FORMAT csv, HEADER)").format(
                _ident(schema), _ident(table))) as copy:
            for chunk in copy:
                handle.write(chunk)
    return path


def _drop_retired(conn, found: Survey, folder: Path) -> list[str]:
    from psycopg import sql

    done = []
    for schema, table, reason, rows in found.retired:
        path = _backup(conn, schema, table, folder)
        conn.execute(sql.SQL("DROP TABLE {}.{}").format(_ident(schema), _ident(table)))
        done.append(f"dropped {schema}.{table} ({reason}); its {rows} rows are in {path}")
    return done


def apply(conn, found: Survey, backups: Path) -> list[str]:
    return _move_cleansed(conn, found) + _retire_orphans(conn, found) + _drop_retired(conn, found, backups)


def _lock(conn) -> bool:
    """Both pipeline locks for this transaction, or False when a run holds one."""
    return all(conn.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", [key]).fetchone()[0]
               for key in ("ahi-bronze", "ahi-silver"))


def report(found: Survey) -> list[str]:
    lines = []
    for bronze, schema, table in found.cleansed:
        lines.append(f"cleansed table {schema}.{table} (for {bronze}) is outside {config.CLEANSED_SCHEMA}")
    for name, live in found.orphans.items():
        lines.append(f"bronze registration {name} has no table in {config.BRONZE_SCHEMA} ({live} live loads)")
    for schema, table, reason, rows in found.retired:
        lines.append(f"{schema}.{table} is no longer used ({reason}); {rows} rows")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="make the changes (default: only show them)")
    args = parser.parse_args()
    try:
        with db.connection() as conn:
            if not _lock(conn):
                print("A Validate, Ingest or Silver load is running; try again when it has finished.", file=sys.stderr)
                return 1
            found = survey(conn)
            if found.empty:
                print("Nothing to do: the database matches the configured schemas.")
                return 0
            for line in report(found):
                print(f"- {line}")
            if not args.apply:
                print("\nNothing changed. Run again with --apply to make these changes.")
                return 0
            print()
            for line in apply(conn, found, config.WORK_DIR / "backups"):
                print(f"* {line}")
        return 0
    except ApiError as error:
        print(f"{error.message} {error.advice or ''}".strip(), file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
