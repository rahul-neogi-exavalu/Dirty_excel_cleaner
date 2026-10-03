"""The business's reference tables, loaded from the workbooks in ``assets/``.

| Table                              | Workbook                            | Used for                               |
|------------------------------------|-------------------------------------|----------------------------------------|
| ``<control>.division_mapping``     | division_table_details.xlsx         | bronze ``division_name`` by profit center |
| ``<control>.lotl``                 | pc_name_pc_number_from_lotl.xlsx    | Silver profit center name / number     |
| ``<silver>.drt_column_mapping``    | drt_column_mapping.xlsx             | the approved column mapping            |
| ``<control>.drt_label``            | drt_column_mapping.xlsx (DRT_Column)| the DRT labels the business uses       |

A table is filled from its workbook only while it is empty, so rows added or changed
in the database (approved mappings, a corrected LOTL) are never overwritten.
``backend/tools/seed_reference.py --replace`` reloads them on purpose. The tables are
created by the migrations; nothing here runs DDL.

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
LABEL_TABLE = "drt_label"


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


def seed_silver(conn, columns_catalog, replace: bool = False) -> dict[str, int]:
    """Fill the business's DRT labels and the DRT column mapping from their workbook.

    The tables themselves are created by migration 005. ``drt_column_mapping`` holds
    exactly the business's columns plus ``silver_column_name``; one source column may map
    to two Silver columns, so there is no primary key and a unique index stops duplicates.
    Each table is filled only while it is empty, unless ``replace``. The first time the
    labels are loaded, labels the app once invented for other Silver columns are cleared.
    """
    loaded = {}
    if replace or _empty(conn, config.CONTROL_SCHEMA, LABEL_TABLE):
        labels = drt_labels_from_file()
        if labels:
            loaded[f"{config.CONTROL_SCHEMA}.{LABEL_TABLE}"] = _insert(
                conn, config.CONTROL_SCHEMA, LABEL_TABLE, ("label",), [(label,) for label in labels], replace)
            cleared = clear_unknown_labels(conn)
            if cleared:
                log.info("DRT column mapping: %s label(s) the business does not use were cleared", cleared)
    if replace or _empty(conn, config.SILVER_SCHEMA, DRT_TABLE):
        rows = [row for row in drt_rows_from_file(columns_catalog) if row[0] and row[1]]
        # The workbook repeats a few rows exactly; the unique index would refuse them.
        rows = list(dict.fromkeys(rows))
        if rows:
            loaded[f"{config.SILVER_SCHEMA}.{DRT_TABLE}"] = _insert(
                conn, config.SILVER_SCHEMA, DRT_TABLE, DRT_COLUMNS, rows, replace)
    if loaded:
        log.info("Reference data loaded: %s", loaded)
    return loaded


def drt_labels_from_file() -> list[str]:
    """The workbook's distinct DRT_Column values, trimmed, in first-seen order."""
    labels = (drt.strip() for _, _, drt in read_workbook(DRT_FILE, 3, keep_text=True) if drt and drt.strip())
    return list(dict.fromkeys(labels))


def business_labels(conn) -> list[str]:
    """The DRT labels the business uses."""
    from psycopg import sql

    if conn.execute("SELECT to_regclass(%s)", [f'"{config.CONTROL_SCHEMA}"."{LABEL_TABLE}"']).fetchone()[0] is None:
        return []
    return [row[0] for row in conn.execute(sql.SQL("SELECT label FROM {}.{} ORDER BY label").format(
        _ident(config.CONTROL_SCHEMA), _ident(LABEL_TABLE)))]


def drt_labels(conn, columns_catalog) -> dict[str, str]:
    """Silver column -> the business's DRT label for it, for the columns that have one."""
    from ahi_silver.catalog import silver_name_for_drt

    found = {}
    for label in business_labels(conn):
        silver = silver_name_for_drt(label, columns_catalog)
        if silver:
            found.setdefault(silver, label)
    return found


def clear_unknown_labels(conn) -> int:
    """Set drt_column to NULL where it is not one of the business's labels.

    The app used to write the Silver catalog's label for every mapped column ("Producer
    Office Zipcode"), which the business never uses. A row whose twin (same profit center,
    source column and Silver column) would then be identical is deleted instead.
    """
    from psycopg import sql

    from ahi_silver.catalog import loose

    known = {loose(label) for label in business_labels(conn)}
    if not known:
        return 0
    table = sql.SQL("{}.{}").format(_ident(config.SILVER_SCHEMA), _ident(DRT_TABLE))
    present = [row[0] for row in conn.execute(sql.SQL("SELECT DISTINCT drt_column FROM {} WHERE drt_column IS NOT NULL")
                                              .format(table))]
    unknown = [label for label in present if loose(label) not in known]
    if not unknown:
        return 0
    # A twin with no label or the business's label is kept; of two unknown-label twins, the first.
    conn.execute(sql.SQL(
        "DELETE FROM {t} a USING {t} b WHERE a.drt_column = ANY(%s) AND a.ctid <> b.ctid "
        "AND a.profit_center = b.profit_center AND a.pc_column = b.pc_column "
        "AND a.silver_column_name IS NOT DISTINCT FROM b.silver_column_name "
        "AND (b.drt_column IS NULL OR NOT (b.drt_column = ANY(%s)) OR b.ctid < a.ctid)").format(t=table),
        [unknown, unknown])
    return conn.execute(sql.SQL("UPDATE {} SET drt_column = NULL WHERE drt_column = ANY(%s)").format(table),
                        [unknown]).rowcount


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
