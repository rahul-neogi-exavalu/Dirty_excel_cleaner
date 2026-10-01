"""word2vec similarity between column names, without gensim.

Vectors are read straight from a word2vec file -- text (`.txt`, `.vec`) or binary
(`.bin`, the original Google format) -- with numpy, so the step works on any Python the
app runs on. Only the first `limit` words are kept: word2vec files list words by
frequency, and the full Google News model would need several GB.

A name's vector is the mean of its words' vectors; similarity is cosine.
"""

from __future__ import annotations

import gzip
import threading
from pathlib import Path

import numpy as np

_cache: dict[tuple[str, int], "Vectors"] = {}
_lock = threading.Lock()


class Vectors:
    def __init__(self, table: dict[str, np.ndarray]):
        self.table = table

    def phrase(self, words) -> np.ndarray | None:
        """Mean vector of the words -- or None if any word is unknown.

        Averaging only the known words silently drops the unknown ones, and those are
        usually the meaningful part (an abbreviation like "incp"); "acc effective date"
        would read as plain "effective date". An incomplete phrase is no evidence at all.
        """
        words = [word for word in words if word]
        if not words or any(word not in self.table for word in words):
            return None
        found = [self.table[word] for word in words]
        vector = np.mean(found, axis=0)
        norm = np.linalg.norm(vector)
        return vector / norm if norm else None

    def similarity(self, left, right) -> float | None:
        a, b = self.phrase(left), self.phrase(right)
        if a is None or b is None:
            return None
        return float(np.dot(a, b))


def load(path: str, limit: int = 200_000) -> Vectors:
    """Read (and cache) a word2vec file."""
    key = (str(path), limit)
    with _lock:
        if key not in _cache:
            file = Path(path)
            name = file.name.lower()
            # gensim-data ships the Google News model gzipped: read it streaming, never
            # unpacked (only the first `limit` words are taken anyway).
            if name.endswith(".gz"):
                binary = not name.endswith((".txt.gz", ".vec.gz"))
                opener = lambda: gzip.open(file, "rb" if binary else "rt", encoding=None if binary else "utf-8", errors=None if binary else "ignore")  # noqa: E731
            else:
                binary = name.endswith(".bin")
                opener = lambda: open(file, "rb") if binary else open(file, encoding="utf-8", errors="ignore")  # noqa: E731
            with opener() as handle:
                table = _read_binary(handle, limit) if binary else _read_text(handle, limit)
            _cache[key] = Vectors(table)
        return _cache[key]


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


def _read_text(handle, limit: int) -> dict[str, np.ndarray]:
    table: dict[str, np.ndarray] = {}
    first = handle.readline()
    head = first.split()
    # word2vec text files start with "<count> <dims>"; GloVe-style files do not.
    lines = handle if (len(head) == 2 and all(part.isdigit() for part in head)) else _chain(first, handle)
    for line in lines:
        parts = line.rstrip().split(" ")
        if len(parts) < 3:
            continue
        word = parts[0].casefold()
        if word not in table:
            table[word] = _normalize(np.asarray(parts[1:], dtype=np.float32))
        if len(table) >= limit:
            break
    return table


def _chain(first: str, rest):
    yield first
    yield from rest


def _read_binary(handle, limit: int) -> dict[str, np.ndarray]:
    table: dict[str, np.ndarray] = {}
    count, dims = (int(part) for part in handle.readline().split())
    width = np.dtype(np.float32).itemsize * dims
    for _ in range(min(count, limit)):
        word = bytearray()
        while (char := handle.read(1)) not in (b" ", b""):
            if char != b"\n":
                word.extend(char)
        vector = np.frombuffer(handle.read(width), dtype=np.float32)
        key = word.decode("utf-8", errors="ignore").casefold()
        if key and key not in table:
            table[key] = _normalize(vector)
    return table
