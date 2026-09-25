"""HTTP service in front of the cleaner.

Everything here delegates to ``ahi_clean``; nothing in ``src/`` is modified. The API adds
what a browser needs and a CLI does not: uploads, background jobs with progress, a
paginated preview, header renames, and on-demand exports.
"""

import sys
from pathlib import Path

# Same trick as clean.py: make ``ahi_clean`` importable without installing it.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
