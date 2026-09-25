#!/usr/bin/env python
"""Entry point: run the cleaner from the project root without setting PYTHONPATH.

    python clean.py "sample_files_uncleaned/*.xlsx"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ahi_clean.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
