"""SQLite WAL + FTS5 v2 memory engine for the QQ bot.

Zero required external dependencies — built purely on the Python 3 standard library.
"""
from __future__ import annotations

import importlib

from . import (
    consolidate,
    continuity,
    db,
    embedder,
    ingest,
    quality_gate,
    retrieve,
    sleep,
    tokenizer,
)

__all__ = [
    "consolidate",
    "continuity",
    "db",
    "embedder",
    "ingest",
    "migrate_v1",
    "quality_gate",
    "retrieve",
    "sleep",
    "tokenizer",
]


def __getattr__(name: str):
    # Lazy-load CLI utility module so `python3 -m memory_engine.migrate_v1` emits no RuntimeWarning.
    if name == "migrate_v1":
        mod = importlib.import_module(".migrate_v1", __name__)
        globals()["migrate_v1"] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
