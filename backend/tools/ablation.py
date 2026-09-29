#!/usr/bin/env python
"""Disable each signal in turn and re-score the corpus, to see what is load-bearing.

    python tools/ablation.py

Kept as a tool rather than a hand-run snippet for the same reason the scorecard is:
a result quoted in a document goes stale the moment the corpus changes, and a stale
measurement is worse than none because it still reads as evidence.

**A PASS means "no workbook in the current corpus depends on this", not "this signal is
useless".** Ablation measures the corpus as much as the code. Reading it otherwise would
justify deleting the only thing standing between a past failure and a repeat of it, so
the size of the corpus is printed alongside the result and belongs with any quotation of
it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import scorecard  # noqa: E402

from ahi_clean import extract, geometry, header, orchestrate, pivot, rowclass, signals  # noqa: E402

NEVER_MATCHES = re.compile(r"(?!x)x")


def corpus():
    return sorted(
        path for path in scorecard.SAMPLES.glob("*.xlsx") if "master" not in path.name.lower()
    )


def score(paths) -> list[str]:
    """Which workbooks come out wrong with the current configuration."""
    broken = []
    for path in paths:
        try:
            outcome = scorecard.evaluate(path)
            if outcome["issues"]:
                broken.append(path.stem.split("_")[0])
        except Exception as error:  # noqa: BLE001 - a crash is a failure like any other
            # Marked distinctly: a crash usually means the ablation itself is broken,
            # not that the signal was load-bearing, and the two must not read alike.
            broken.append(f"{path.stem.split('_')[0]}!{type(error).__name__}")
    return broken


def weights_without(name):
    """Header weights with one signal zeroed, the rest scaled back up to 1.0.

    Zeroed rather than removed: the scorer looks every component up by name, so dropping
    a key raises instead of measuring anything.
    """
    remaining = {key: value for key, value in header.WEIGHTS.items() if key != name}
    total = sum(remaining.values())
    scaled = {key: value / total for key, value in remaining.items()}
    scaled[name] = 0.0
    return scaled


def ablations():
    """Each entry disables one signal and yields a restore callback."""

    def header_weight(name):
        def apply():
            original = dict(header.WEIGHTS)
            # Computed before the clear: reading the dict after emptying it would
            # measure nothing and quietly report a crash as a finding.
            replacement = weights_without(name)
            header.WEIGHTS.clear()
            header.WEIGHTS.update(replacement)
            return lambda: (header.WEIGHTS.clear(), header.WEIGHTS.update(original))

        return apply

    def swap(module, attribute, replacement):
        def apply():
            original = getattr(module, attribute)
            setattr(module, attribute, replacement)
            return lambda: setattr(module, attribute, original)

        return apply

    return [
        ("baseline (all signals)", lambda: (lambda: None)),
        ("no type-contrast in header", header_weight("contrast")),
        ("no uniqueness in header", header_weight("uniqueness")),
        ("no fill-ratio in header", header_weight("fill_ratio")),
        ("no arithmetic total check", swap(rowclass, "_confirm_totals", lambda body, verdicts: None)),
        ("no sparsity check", swap(rowclass, "SPARSE_RATIO", 0.0)),
        ("no total/footer text patterns", swap(rowclass, "_TOTAL_HINT", NEVER_MATCHES)),
        ("no restated-header detection", swap(rowclass, "_reads_as_a_header", lambda row, profile: False)),
        ("no repeated-header detection", swap(rowclass, "_matches_header", lambda row, norm: False)),
        ("no sheet-level orientation", swap(extract, "_orient_sheet", lambda rows: (rows, False))),
        ("no header/body realignment", swap(extract, "_realign_gutters", lambda n, f, r, b, w, t: (n, b, w))),
        ("no region profile matching", swap(geometry, "PROFILE_MATCH_THRESHOLD", 2.0)),
        ("no column-extent splitting", swap(geometry, "EXTENT_MATCH_THRESHOLD", 0.0)),
        ("no merged-banner detection", swap(signals, "is_merged_banner", lambda cells: False)),
        ("no pivot detection", swap(pivot, "detect", lambda rows, index: None)),
        ("no header adoption", swap(orchestrate, "adopt_sibling_headers", lambda results: [])),
    ]


def main() -> int:
    paths = corpus()
    if not paths:
        print("no scenario workbooks found in sample_files_uncleaned/", file=sys.stderr)
        return 1

    print(f"ablation over {len(paths)} workbook(s)\n")
    print(f"| {'ablation':<32} | {'result':<34} |")
    print(f"|{'-' * 34}|{'-' * 36}|")

    for label, apply in ablations():
        restore = apply()
        try:
            broken = score(paths)
        finally:
            restore()
        result = "PASS" if not broken else f"BREAKS {len(broken)} ({', '.join(sorted(set(broken)))})"
        print(f"| {label:<32} | {result:<34} |")

    print(
        f"\nA PASS means no workbook in this {len(paths)}-workbook corpus depends on that "
        "signal.\nIt does not mean the signal is unnecessary."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
