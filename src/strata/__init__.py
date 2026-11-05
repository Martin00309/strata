"""Strata — an embedded, ordered, crash-safe key-value storage engine.

Strata is a log-structured merge-tree (LSM) storage engine written in pure
Python with no runtime dependencies. It provides an ordered ``bytes`` → ``bytes``
map with durable writes, atomic batches, consistent snapshots and range scans,
backed by a write-ahead log, an in-memory memtable and immutable on-disk
sorted-string tables (SSTables) organised into levels.

The public surface is intentionally small::

    import strata

    db = strata.open("./mydb")
    db.put(b"key", b"value")
    print(db.get(b"key"))        # b"value"
    for key, value in db.scan(start=b"a", end=b"m"):
        ...
    db.close()

See the module docstrings for the internals (``strata.wal``, ``strata.sstable``,
``strata.compaction``, …).
"""

from __future__ import annotations

from .batch import WriteBatch
from .db import DB, Snapshot
from .errors import (
    Corruption,
    DBClosed,
    InvalidArgument,
    LockError,
    NotSupported,
    StrataError,
)
from .options import Options, ReadOptions, WriteOptions

__version__ = "0.1.0"


def open(name: str, options: Options | None = None) -> DB:  # noqa: A001
    """Open (or create) the Strata database rooted at directory ``name``.

    The returned :class:`~strata.db.DB` is also a context manager::

        with strata.open("./mydb") as db:
            db.put(b"k", b"v")
    """
    return DB.open(name, options)


__all__ = [
    "__version__",
    "open",
    "DB",
    "Snapshot",
    "WriteBatch",
    "Options",
    "ReadOptions",
    "WriteOptions",
    "StrataError",
    "Corruption",
    "InvalidArgument",
    "NotSupported",
    "DBClosed",
    "LockError",
]
