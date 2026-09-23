#!/usr/bin/env python
"""Entry point: run the cleaner from the project root without setting PYTHONPATH.

    python clean.py "sample_files_uncleaned/*.xlsx"
"""

# This file runs the existing workbook cleaning pipeline from the project root.
# After cleaning, it also creates transposed metadata CSVs for each cleaned file.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ahi_clean.cli import main  # noqa: E402
from clean_metadata import generate_metadata_from_args  # noqa: E402

if __name__ == "__main__":
    arguments = sys.argv[1:]
    exit_code = main(arguments)
    try:
        generate_metadata_from_args(arguments)
    except OSError as error:
        print(f"could not write cleaned-file metadata: {error}", file=sys.stderr)
    raise SystemExit(exit_code)
