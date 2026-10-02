"""The Silver detail columns, from a CSV that mirrors the business's silver_schema.

Every column of the ``silver_detail`` table is listed, in table order, with its type
exactly as the schema states it (``string``, ``int``, ``bigint``, ``boolean``, ``date``,
``timestamp``, ``decimal(18,2)``...). ``role`` says who fills it:

* ``mapped`` -- a bronze column is mapped onto it (the targets the review offers);
* ``system`` -- the pipeline fills it (keys, hashes, lineage, timestamps, periods).

``drt_column_name`` is the business's DRT label where one exists ("InsuranceCompany
Name"); it is what the DRT column mapping table uses, and a second name to match against.
The description lists synonyms, in parentheses, for the semantic and AI votes.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

MAPPED, SYSTEM = "mapped", "system"
_SIMPLE = {"string", "date", "int", "bigint", "boolean", "timestamp"}
_DECIMAL = re.compile(r"decimal\((\d+),\s*(\d+)\)")
# Older catalogs used these names.
_ALIASES = {"text": "string", "decimal": "decimal(18,2)", "integer": "int"}


@dataclass(frozen=True)
class SilverColumn:
    name: str
    drt_name: str
    data_type: str
    business_key: bool
    description: str
    role: str = MAPPED

    @property
    def kind(self) -> str:
        """string | date | int | bigint | boolean | timestamp | decimal."""
        return "decimal" if self.data_type.startswith("decimal") else self.data_type

    @property
    def scale(self) -> int:
        match = _DECIMAL.fullmatch(self.data_type)
        return int(match.group(2)) if match else 0

    @property
    def precision(self) -> int:
        match = _DECIMAL.fullmatch(self.data_type)
        return int(match.group(1)) if match else 0

    @property
    def sql_type(self) -> str:
        """The Postgres type. ``timestamp`` is an instant, so ``timestamptz``."""
        if self.kind == "decimal":
            return f"numeric({self.precision},{self.scale})"
        return {"string": "text", "int": "integer", "bigint": "bigint", "boolean": "boolean",
                "date": "date", "timestamp": "timestamptz"}[self.kind]


def normalize_type(value: str) -> str | None:
    data_type = re.sub(r"\s+", "", (value or "string").strip().lower())
    data_type = _ALIASES.get(data_type, data_type)
    if data_type in _SIMPLE:
        return data_type
    match = _DECIMAL.fullmatch(data_type)
    return f"decimal({match.group(1)},{match.group(2)})" if match else None


def load(path: Path) -> list[SilverColumn]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        columns = []
        for row in csv.DictReader(handle):
            name = (row.get("silver_column_name") or "").strip()
            if not name:
                continue
            data_type = normalize_type(row.get("data_type") or "string")
            if data_type is None:
                raise ValueError(f"{path.name}: {name} has unknown data_type '{row.get('data_type')}' "
                                 "(use string, int, bigint, boolean, date, timestamp or decimal(p,s))")
            role = (row.get("role") or MAPPED).strip().lower()
            if role not in (MAPPED, SYSTEM):
                raise ValueError(f"{path.name}: {name} has unknown role '{role}' (use mapped or system)")
            columns.append(SilverColumn(
                name=name,
                drt_name=(row.get("drt_column_name") or "").strip(),
                data_type=data_type,
                business_key=(row.get("business_key") or "").strip().lower() in ("y", "yes", "true", "1"),
                description=(row.get("description") or "").strip(),
                role=role,
            ))
    names = [column.name for column in columns]
    if not names:
        raise ValueError(f"{path.name} lists no Silver columns")
    if len(set(names)) != len(names):
        raise ValueError(f"{path.name}: silver_column_name values must be unique")
    if not any(column.role == MAPPED for column in columns):
        raise ValueError(f"{path.name} lists no mapped Silver columns")
    return columns


def load_plain(path: Path) -> list[SilverColumn]:
    """A table's columns from ``column_name,data_type[,description]`` (silver_aggregate)."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        columns = []
        for row in csv.DictReader(handle):
            name = (row.get("column_name") or "").strip()
            if not name:
                continue
            data_type = normalize_type(row.get("data_type") or "string")
            if data_type is None:
                raise ValueError(f"{path.name}: {name} has unknown data_type '{row.get('data_type')}'")
            columns.append(SilverColumn(name, "", data_type, False, (row.get("description") or "").strip(), SYSTEM))
    if not columns:
        raise ValueError(f"{path.name} lists no columns")
    return columns


def targets(columns: list[SilverColumn]) -> list[SilverColumn]:
    """The columns a bronze column can be mapped onto."""
    return [column for column in columns if column.role == MAPPED]


def by_drt_name(columns: list[SilverColumn]) -> dict[str, str]:
    """DRT label -> silver column name, matched loosely (case, spaces, punctuation)."""
    return {_loose(column.drt_name): column.name for column in columns if column.drt_name}


def _loose(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold())


def silver_name_for_drt(drt_column: str | None, columns: list[SilverColumn]) -> str | None:
    """The silver column a DRT label means: 'InsuranceCompany Name' -> insurance_company_name."""
    if not drt_column:
        return None
    return by_drt_name(columns).get(_loose(drt_column))
