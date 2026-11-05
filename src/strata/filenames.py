"""Canonical names for the files that make up a database directory.

A Strata database is a directory holding:

* ``CURRENT``            — one line naming the live ``MANIFEST-*`` file,
* ``MANIFEST-NNNNNN``    — the log of version edits (the metadata journal),
* ``NNNNNN.log``         — write-ahead logs,
* ``NNNNNN.sst``         — immutable sorted tables, and
* ``LOCK``               — a lock file held while the database is open.

File numbers are drawn from one monotonically increasing counter, so a single
number is unique across every file kind.
"""

from __future__ import annotations

import os


def _numbered(dbname: str, number: int, suffix: str) -> str:
    return os.path.join(dbname, f"{number:06d}{suffix}")


def log_file_name(dbname: str, number: int) -> str:
    return _numbered(dbname, number, ".log")


def table_file_name(dbname: str, number: int) -> str:
    return _numbered(dbname, number, ".sst")


def descriptor_file_name(dbname: str, number: int) -> str:
    return os.path.join(dbname, f"MANIFEST-{number:06d}")


def current_file_name(dbname: str) -> str:
    return os.path.join(dbname, "CURRENT")


def lock_file_name(dbname: str) -> str:
    return os.path.join(dbname, "LOCK")


def temp_file_name(dbname: str, number: int) -> str:
    return _numbered(dbname, number, ".dbtmp")


__all__ = [
    "log_file_name",
    "table_file_name",
    "descriptor_file_name",
    "current_file_name",
    "lock_file_name",
    "temp_file_name",
]
