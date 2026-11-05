"""Exception hierarchy for Strata.

All errors raised by the engine derive from :class:`StrataError`, so callers
can catch everything with a single ``except StrataError``.
"""

from __future__ import annotations


class StrataError(Exception):
    """Base class for every error raised by Strata."""


class Corruption(StrataError):
    """On-disk data failed an integrity check (bad CRC, truncated block, …).

    Raised during reads and recovery when ``paranoid_checks`` is enabled or
    when a structurally invalid record is encountered that cannot be treated
    as a benign truncated tail.
    """


class InvalidArgument(StrataError):
    """A caller supplied an argument the engine cannot accept."""


class NotSupported(StrataError):
    """The requested operation is recognised but not implemented yet."""


class DBClosed(StrataError):
    """An operation was attempted on a database that has been closed."""


class LockError(StrataError):
    """The database directory is already locked by another handle/process."""


__all__ = [
    "StrataError",
    "Corruption",
    "InvalidArgument",
    "NotSupported",
    "DBClosed",
    "LockError",
]
