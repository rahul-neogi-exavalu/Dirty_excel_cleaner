#!/usr/bin/env python
"""Entry point: run the cleaner from the project root without setting PYTHONPATH.

    python clean.py                          # every sample in backend/sample_files_uncleaned
    python clean.py "path/to/*.xlsx"         # your own files, relative to where you run it

The cleaner lives in backend/src. Its own defaults (sample folder, cleaned/, audit/) are
relative to the working directory, so when an input or an output folder is not given
here they are filled in pointing at backend/, where those folders live.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND / "src"))

from ahi_clean.cli import main  # noqa: E402

# Options whose next token is a value rather than an input path.
_VALUED = {"--out", "--audit", "--workers", "--executor", "--max-cells"}


def _with_backend_defaults(argv: list[str]) -> list[str]:
    given, positional, skip = set(), False, False
    for token in argv:
        if skip:
            skip = False
            continue
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            given.add(name)
            skip = name in _VALUED and "=" not in token
        elif not token.startswith("-"):
            positional = True

    args = list(argv)
    if "--out" not in given:
        args += ["--out", str(BACKEND / "cleaned")]
    if "--audit" not in given:
        args += ["--audit", str(BACKEND / "audit")]
    if not positional and not ({"-h", "--help"} & set(argv)):
        samples = BACKEND / "sample_files_uncleaned"
        args += [str(samples / "*.xlsx"), str(samples / "*.csv"), str(samples / "*.tsv")]
    return args


if __name__ == "__main__":
    raise SystemExit(main(_with_backend_defaults(sys.argv[1:])))
