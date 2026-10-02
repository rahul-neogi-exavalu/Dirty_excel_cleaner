"""The business's reference tables, loaded from the workbooks in ``assets/``.

| Table                              | Workbook                            | Used for                               |
|------------------------------------|-------------------------------------|----------------------------------------|
| ``<control>.division_mapping``     | division_table_details.xlsx         | bronze ``division_name`` by profit center |
| ``<control>.lotl``                 | pc_name_pc_number_from_lotl.xlsx    | Silver profit center name / number     |
| ``<silver>.drt_column_mapping``    | drt_column_mapping.xlsx             | the approved column mapping            |

A table is filled from its workbook only while it is empty, so rows added or changed
in the database (approved mappings, a corrected LOTL) are never overwritten.
``backend/tools/seed_reference.py --replace`` reloads them on purpose.

The LOTL is loaded exactly as the workbook holds it: every row, every cell as written
(the text ``null`` stays ``null``, line breaks in names stay), under the workbook's own
column names. The lookup treats ``null`` as missing. In the division table the text
``null`` means a missing value. The DRT mapping's source column names are kept exactly as
written (some carry tabs or line breaks; matching normalises them).
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import config

log = logging.getLogger("ahi.reference")

DIVISION_FILE = "division_table_details.xlsx"
LOTL_FILE = "pc_name_pc_number_from_lotl.xlsx"
DRT_FILE = "drt_column_mapping.xlsx"

DIVISION_COLUMNS = ("division", "international_office", "profit_center")
LOTL_COLUMNS = ("profit_center_number", "legacy_office_name", "status")
DRT_COLUMNS = ("profit_center", "pc_column", "drt_column", "silver_column_name")
DRT_TABLE = "drt_column_mapping"


def _ident(name: str):
    from psycopg import sql

    return sql.Identifier(name)


def _null(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.casefold() == "null" else text


def read_workbook(name: str, width: int, keep_text: bool = False, exact: tuple[str, ...] | None = None) -> list[tuple]:
    """The first sheet's rows below the header, ``width`` cells each. Missing file: [].

    ``exact``: the header the sheet must have. Cells are then kept exactly as written
    (no trimming, the text ``null`` kept); only a row with no cell at all is skipped.
    """
    path = Path(config.REFERENCE_DIR) / name
    if not path.exists():
        log.warning("Reference workbook %s not found; the table is left as it is.", path)
        return []
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        rows = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            cells = list(row[:width]) + [None] * (width - len(row[:width]))
            if index == 0:
                header = tuple(str(c).strip() if c is not None else "" for c in cells)
                if exact and header != exact:
                    raise ValueError(f"{name}: expected the columns {', '.join(exact)}, found {', '.join(header)}")
                continue
            if exact:
                values = tuple(None if c is None else str(c) for c in cells)
            elif keep_text:
                values = tuple(None if c is None or str(c).strip() == "" else str(c) for c in cells)
            else:
                values = tuple(_null(c) for c in cells)
            if any(value is not None for value in values):
                rows.append(values)
        return rows
    finally:
        workbook.close()


def division_rows() -> list[tuple]:
    return read_workbook(DIVISION_FILE, 3)


def lotl_rows_from_file() -> list[tuple]:
    """Every row of pc_name_pc_number_from_lotl.xlsx, exactly as written."""
    return read_workbook(LOTL_FILE, 3, exact=LOTL_COLUMNS)


def drt_rows_from_file(columns_catalog) -> list[tuple]:
    """(profit_center, pc_column, drt_column, silver_column_name), the last derived from
    the DRT label through the Silver catalog ('InsuranceCompany Name' -> insurance_company_name)."""
    from ahi_silver.catalog import silver_name_for_drt

    rows = []
    for profit_center, pc_column, drt_column in read_workbook(DRT_FILE, 3, keep_text=True):
        rows.append((profit_center.strip() if profit_center else None, pc_column, drt_column.strip() if drt_column else None,
                     silver_name_for_drt(drt_column, columns_catalog)))
    return rows


def _lotl_ref() -> tuple[str, str]:
    """AHI_LOTL_TABLE ([schema.]table), or the control schema's ``lotl``."""
    if not config.LOTL_TABLE:
        return config.CONTROL_SCHEMA, "lotl"
    schema, _, table = config.LOTL_TABLE.rpartition(".")
    return (schema or config.CONTROL_SCHEMA), table


def lotl_name() -> str:
    return ".".join(_lotl_ref())


def _empty(conn, schema: str, table: str) -> bool:
    from psycopg import sql

    if conn.execute("SELECT to_regclass(%s)", [f'"{schema}"."{table}"']).fetchone()[0] is None:
        return False  # not there: nothing to fill (a LOTL kept elsewhere is not ours to create)
    return conn.execute(sql.SQL("SELECT NOT EXISTS (SELECT 1 FROM {}.{})").format(_ident(schema), _ident(table))).fetchone()[0]


def _insert(conn, schema: str, table: str, columns: tuple[str, ...], rows: list[tuple], replace: bool) -> int:
    from psycopg import sql

    target = sql.SQL("{}.{}").format(_ident(schema), _ident(table))
    if replace:
        conn.execute(sql.SQL("DELETE FROM {}").format(target))
    with conn.cursor() as cursor, cursor.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(
            target, sql.SQL(", ").join(_ident(c) for c in columns))) as copy:
        for row in rows:
            copy.write_row(row)
    return len(rows)


def seed_control(conn, replace: bool = False) -> dict[str, int]:
    """Fill division_mapping and the LOTL from their workbooks (only if empty, unless ``replace``)."""
    loaded = {}
    if replace or _empty(conn, config.CONTROL_SCHEMA, "division_mapping"):
        rows = division_rows()
        if rows:
            loaded["division_mapping"] = _insert(conn, config.CONTROL_SCHEMA, "division_mapping", DIVISION_COLUMNS, rows, replace)
    schema, table = _lotl_ref()
    if replace or _empty(conn, schema, table):
        rows = lotl_rows_from_file()
        if rows:
            loaded[f"{schema}.{table}"] = _insert(conn, schema, table, LOTL_COLUMNS, rows, replace)
    if loaded:
        log.info("Reference data loaded: %s", loaded)
    return loaded


def ensure_drt_table(conn, columns_catalog, replace: bool = False) -> int:
    """Create ``<silver>.drt_column_mapping`` if needed and fill it from its workbook when empty.

    Exactly the business's columns plus ``silver_column_name``. One source column may map
    to two Silver columns, so there is no primary key; a unique index stops duplicates.
    An ignored column is a row with no drt_column and no silver_column_name.
    """
    from psycopg import sql

    schema = _ident(config.SILVER_SCHEMA)
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
    conn.execute(sql.SQL(
        "CREATE TABLE IF NOT EXISTS {}.{} (profit_center text NOT NULL, pc_column text NOT NULL, "
        "drt_column text, silver_column_name text)").format(schema, _ident(DRT_TABLE)))
    conn.execute(sql.SQL(
        "CREATE UNIQUE INDEX IF NOT EXISTS drt_column_mapping_unique ON {}.{} "
        "(profit_center, pc_column, coalesce(silver_column_name, ''), coalesce(drt_column, ''))").format(
            schema, _ident(DRT_TABLE)))
    if replace or _empty(conn, config.SILVER_SCHEMA, DRT_TABLE):
        rows = [row for row in drt_rows_from_file(columns_catalog) if row[0] and row[1]]
        # The workbook repeats a few rows exactly; the unique index would refuse them.
        rows = list(dict.fromkeys(rows))
        if rows:
            count = _insert(conn, config.SILVER_SCHEMA, DRT_TABLE, DRT_COLUMNS, rows, replace)
            log.info("DRT column mapping loaded: %s rows", count)
            return count
    return 0


def divisions(conn) -> list[tuple]:
    """Every (division, international_office, profit_center) row."""
    from psycopg import sql

    return conn.execute(sql.SQL("SELECT division, international_office, profit_center FROM {}.division_mapping")
                        .format(_ident(config.CONTROL_SCHEMA))).fetchall()


def lotl(conn) -> list[tuple]:
    """Every (profit_center_number, legacy_office_name, status) row; [] when the table is absent."""
    from psycopg import sql

    schema, table = _lotl_ref()
    if conn.execute("SELECT to_regclass(%s)", [f'"{schema}"."{table}"']).fetchone()[0] is None:
        return []
    return conn.execute(sql.SQL("SELECT profit_center_number, legacy_office_name, status FROM {}.{}")
                        .format(_ident(schema), _ident(table))).fetchall()
