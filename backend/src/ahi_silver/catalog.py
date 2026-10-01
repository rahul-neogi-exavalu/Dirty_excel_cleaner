"""The Silver target columns (the DRT), from a CSV the business maintains.

`silver_column_name` is the authority -- the column created in Silver and the name every
mapping points at. `drt_column_name` is the business's own label, for reference and as a
second name to match against. The description lists synonyms the semantic and Gemini
steps can use.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

TYPES = ("text", "date", "decimal")


@dataclass(frozen=True)
class SilverColumn:
    name: str
    drt_name: str
    data_type: str
    business_key: bool
    description: str


def load(path: Path) -> list[SilverColumn]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        columns = []
        for row in csv.DictReader(handle):
            name = (row.get("silver_column_name") or "").strip()
            if not name:
                continue
            data_type = (row.get("data_type") or "text").strip().lower()
            if data_type not in TYPES:
                raise ValueError(f"{path.name}: {name} has unknown data_type '{data_type}' (use {', '.join(TYPES)})")
            columns.append(SilverColumn(
                name=name,
                drt_name=(row.get("drt_column_name") or "").strip(),
                data_type=data_type,
                business_key=(row.get("business_key") or "").strip().lower() in ("y", "yes", "true", "1"),
                description=(row.get("description") or "").strip(),
            ))
    names = [column.name for column in columns]
    if not names:
        raise ValueError(f"{path.name} lists no Silver columns")
    if len(set(names)) != len(names):
        raise ValueError(f"{path.name}: silver_column_name values must be unique")
    return columns
