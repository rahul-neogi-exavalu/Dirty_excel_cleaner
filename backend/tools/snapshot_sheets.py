#!/usr/bin/env python
"""Record how the cleaner reads every sheet of a folder, and compare two such records.

A change to the structure heuristics (orientation, header, row classification) moves a few
sheets and must not move the rest. This tool freezes what each sheet comes out as, so the
change can be judged by the sheets it touched rather than by the ones it was written for.

    python tools/snapshot_sheets.py snap  <folder> <out.json> [--max-mb 2]
    python tools/snapshot_sheets.py diff  <before.json> <after.json>

``snap`` runs the extraction (no files are written but the snapshot) and records, per sheet
and per table: rows, columns, orientation and what decided it, plus the contract violations
of the workbook. ``diff`` lists every sheet whose record differs, and every workbook whose
violation count changed.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ahi_clean import contracts  # noqa: E402
from ahi_clean.extract import extract_sheet  # noqa: E402
from ahi_clean.reader import read_workbook  # noqa: E402

SUFFIXES = {".xlsx", ".xlsm", ".xls", ".xlsb"}


def snapshot_workbook(path: Path) -> dict:
    results = []
    sheets: dict[str, list] = {}
    for grid in read_workbook(path):
        found = extract_sheet(grid)
        results.extend(found)
        sheets[grid.name] = [
            [
                len(result.frame),
                len(result.frame.columns),
                (result.trace.get("orientation") or {}).get("orientation"),
                (result.trace.get("orientation") or {}).get("decided_by"),
            ]
            for result in found
        ]
    return {"sheets": sheets, "violations": len(contracts.check_all(results))}


def snap(folder: Path, out: Path, max_mb: float) -> None:
    warnings.filterwarnings("ignore")
    record: dict[str, dict] = {}
    files = sorted(
        path for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in SUFFIXES and not path.name.startswith("~$")
        and path.stat().st_size <= max_mb * 1024 * 1024
    )
    for path in files:
        try:
            record[path.name] = snapshot_workbook(path)
        except Exception as error:  # noqa: BLE001 - a file that cannot be read is part of the record
            record[path.name] = {"error": f"{type(error).__name__}: {str(error)[:120]}"}
    out.write_text(json.dumps(record, indent=1), encoding="utf-8")
    print(f"{len(record)} workbooks recorded in {out}")


def diff(before: Path, after: Path) -> None:
    old, new = json.loads(before.read_text(encoding="utf-8")), json.loads(after.read_text(encoding="utf-8"))
    changed = 0
    for name in sorted(set(old) | set(new)):
        one, other = old.get(name), new.get(name)
        if one == other:
            continue
        lines = []
        if one is None or other is None or "error" in one or "error" in other:
            lines.append(f"      {one if one is None or 'error' in one else 'ok'} -> {other if other is None or 'error' in other else 'ok'}")
        else:
            for sheet in sorted(set(one["sheets"]) | set(other["sheets"])):
                # What decided the orientation is recorded but not compared: a sheet that
                # reads the same way up with the same shape has not changed.
                if _shape(one["sheets"].get(sheet)) != _shape(other["sheets"].get(sheet)):
                    lines.append(f"      {sheet[:30]}: {_shape(one['sheets'].get(sheet))} -> {_shape(other['sheets'].get(sheet))}")
            if one["violations"] != other["violations"]:
                lines.append(f"      contract violations: {one['violations']} -> {other['violations']}")
        if lines:
            changed += 1
            print(f"- {name}")
            print("\n".join(lines))
    print(f"\n{changed} of {len(set(old) | set(new))} workbooks changed")


def _shape(tables) -> str:
    if tables is None:
        return "absent"
    return ", ".join(f"{rows}x{columns} {orientation or '-'}" for rows, columns, orientation, _ in tables) or "no table"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("snap")
    one.add_argument("folder", type=Path)
    one.add_argument("out", type=Path)
    one.add_argument("--max-mb", type=float, default=2.0, help="skip workbooks larger than this")
    two = sub.add_parser("diff")
    two.add_argument("before", type=Path)
    two.add_argument("after", type=Path)
    args = parser.parse_args()
    if args.command == "snap":
        snap(args.folder, args.out, args.max_mb)
    else:
        diff(args.before, args.after)


if __name__ == "__main__":
    main()
