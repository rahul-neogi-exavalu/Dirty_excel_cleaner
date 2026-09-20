"""Classify why a workbook could not be processed.

A batch tool is judged by what it does with the bad file, not the good ones. One
unreadable workbook in a folder of five hundred must cost you that one workbook and
nothing else, and the reason has to be specific enough to act on -- "corrupt" and
"password-protected" call for completely different responses from whoever sent it.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

CORRUPT = "corrupt"
ENCRYPTED = "encrypted"
UNSUPPORTED_FORMAT = "unsupported_format"
MISSING = "missing"
NO_TABLE = "no_table"
TOO_LARGE = "too_large"
UNEXPECTED = "unexpected"

# Formats that are legitimately spreadsheets but that openpyxl cannot open. Worth naming
# individually so the message can say what to convert from, rather than "unreadable".
_FOREIGN_SUFFIXES = {
    ".xls": "legacy Excel 97-2003",
    ".xlsb": "binary Excel",
    ".ods": "OpenDocument",
}

_ADVICE = {
    CORRUPT: "the file is not a readable .xlsx (truncated download, or renamed from another format)",
    ENCRYPTED: "the file is password-protected; it must be opened and re-saved without a password",
    UNSUPPORTED_FORMAT: "this format cannot be read directly; re-save it as .xlsx",
    MISSING: "the file no longer exists",
    NO_TABLE: "the workbook opened but held nothing table-shaped",
    TOO_LARGE: "the sheet exceeds the configured cell ceiling",
    UNEXPECTED: "unexpected internal error; see the traceback written beside the audit reports",
}


@dataclass
class Failure:
    """One workbook that could not be processed, and why."""

    source: Path
    kind: str
    detail: str

    @property
    def advice(self) -> str:
        return _ADVICE.get(self.kind, _ADVICE[UNEXPECTED])

    def as_dict(self) -> dict:
        return {
            "file": self.source.name,
            "path": str(self.source),
            "kind": self.kind,
            "detail": self.detail,
            "advice": self.advice,
        }


def classify(source: Path, error: BaseException) -> Failure:
    """Work out which kind of failure an exception represents.

    Classification leans on the exception first and the file name second. A
    password-protected .xlsx is an OLE container rather than a zip, so openpyxl raises
    the same error as for a genuinely corrupt file -- the two are told apart by looking
    at the magic bytes, because telling someone their file is corrupt when it is merely
    locked sends them looking in the wrong place.
    """
    suffix = source.suffix.lower()

    if isinstance(error, FileNotFoundError):
        return Failure(source, MISSING, str(error))

    if suffix in _FOREIGN_SUFFIXES:
        return Failure(
            source,
            UNSUPPORTED_FORMAT,
            f"{_FOREIGN_SUFFIXES[suffix]} ({suffix}) is not supported",
        )

    if isinstance(error, zipfile.BadZipFile):
        if _is_ole_container(source):
            return Failure(source, ENCRYPTED, "file is an OLE container, typical of password protection")
        return Failure(source, CORRUPT, str(error))

    name = type(error).__name__
    if name == "SheetTooLarge":
        return Failure(source, TOO_LARGE, str(error))
    if "InvalidFile" in name:
        return Failure(source, UNSUPPORTED_FORMAT, str(error))
    if "MemoryError" in name:
        return Failure(source, TOO_LARGE, "ran out of memory while reading the workbook")

    return Failure(source, UNEXPECTED, f"{name}: {error}")


def _is_ole_container(source: Path) -> bool:
    """Whether a file starts with the OLE compound-document signature.

    Both password-protected .xlsx files and genuine .xls files are OLE containers. The
    suffix check above has already taken .xls out of the running by the time this is
    consulted, so reaching here with OLE magic means encryption.
    """
    try:
        with open(source, "rb") as handle:
            return handle.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    except OSError:
        return False
