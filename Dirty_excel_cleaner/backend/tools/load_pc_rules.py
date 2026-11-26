"""Load the per-profit-center Silver rules from backend/config/pc_rules/*.yaml.

    python backend/tools/load_pc_rules.py --check      # validate the files only (no database)
    python backend/tools/load_pc_rules.py --dry-run    # what would be added or updated
    python backend/tools/load_pc_rules.py              # add new rules, update rules still as seeded
    python backend/tools/load_pc_rules.py --force      # also overwrite rules a person edited in the UI (!)
    python backend/tools/load_pc_rules.py --export out_folder   # write the stored rules back to YAML

Uses the database in backend/.env (or .env). A rule edited in the Silver page's Rules tab
is recorded as approved by that person and is left as they made it, unless --force is
given. Rules removed from a file are not deleted from the database: disable or delete
them in the UI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from ahi_silver.rules import RuleError  # noqa: E402
from api import db  # noqa: E402
from api.services import pc_rules_service  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", type=Path, default=pc_rules_service.SEEDS, help="the seed folder")
    parser.add_argument("--check", action="store_true", help="validate the files only")
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument("--force", action="store_true", help="overwrite rules edited in the UI")
    parser.add_argument("--export", type=Path, help="write the stored rules to this folder")
    args = parser.parse_args(argv)

    if args.export:
        with db.connection() as conn:
            for path in pc_rules_service.export(conn, args.export):
                print(f"wrote {path}")
        return 0
    try:
        rules = pc_rules_service.read_seeds(args.folder)
    except RuleError as error:
        print(f"The seed files have problems:\n{error}", file=sys.stderr)
        return 1
    print(f"{len(rules)} rules in {len({r.profit_center for r in rules})} profit centers are valid.")
    if args.check:
        return 0
    with db.connection() as conn:
        report = pc_rules_service.load_seeds(conn, rules, force=args.force, dry_run=args.dry_run)
    for name, items in report.items():
        print(f"{name}: {len(items)}")
        if name != "unchanged":
            for item in items:
                print(f"  {item}")
    if args.dry_run:
        print("Dry run: nothing was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
