"""The memtable — the in-memory write buffer at the top of the LSM tree.

Every write first lands in the write-ahead log, then here. A memtable is an
ordered map from *internal key* to value, backed by a skip list. Because
internal keys embed the sequence number and sort newest-first within a user
key, a point lookup is a single seek, and flushing to an SSTable is a straight
in-order walk.

Once a memtable reaches ``write_buffer_size`` it is frozen (made immutable),
a fresh one takes over, and the frozen table is flushed to level 0.
"""

from __future__ import annotations

from .internalkey import (
    TYPE_VALUE,
    compare_internal_keys,
    extract_user_key,
    make_internal_key,
    make_lookup_key,
    parse_internal_key,
)
from .skiplist import SkipList

# Rough per-entry skip-list overhead (node object, pointers, bookkeeping) added
# to the key/value bytes when estimating memory use. Deliberately approximate.
_ENTRY_OVERHEAD = 48


class LookupResult:
    """Outcome of a memtable point lookup.

    * ``FOUND``   — a value is present (``value`` holds it).
    * ``DELETED`` — a tombstone shadows the key; the search must stop here.
    * ``ABSENT``  — the key is not in this memtable; keep searching below.
    """

    FOUND = "found"
    DELETED = "deleted"
    ABSENT = "absent"


class MemTable:
    def __init__(self):
        self._table = SkipList(compare_internal_keys)
        self._memory_usage = 0

    def __len__(self) -> int:
        return len(self._table)

    def empty(self) -> bool:
        return len(self._table) == 0

    def approximate_memory_usage(self) -> int:
        return self._memory_usage

    def add(self, sequence: int, value_type: int, user_key: bytes, value: bytes) -> None:
        """Record a write. ``value`` is ignored for deletions (pass ``b""``)."""
        internal_key = make_internal_key(user_key, sequence, value_type)
        stored = value if value_type == TYPE_VALUE else b""
        self._table.insert(internal_key, stored)
        self._memory_usage += len(internal_key) + len(stored) + _ENTRY_OVERHEAD

    def get(self, user_key: bytes, sequence: int) -> tuple[str, bytes | None]:
        """Look up ``user_key`` as of ``sequence``.

        Returns ``(status, value)`` where ``status`` is a :class:`LookupResult`
        constant. ``value`` is only meaningful when status is ``FOUND``.
        """
        lookup = make_lookup_key(user_key, sequence)
        node_value = None
        found_key = None
        # Walk to the first internal key >= lookup; that is the newest version
        # of some user key with sequence <= the query sequence.
        it = self._table.iterator()
        it.seek(lookup)
        if it.valid():
            found_key = it.key()
            node_value = it.value()
        if found_key is None:
            return LookupResult.ABSENT, None
        candidate_user, _seq, vtype = parse_internal_key(found_key)
        if candidate_user != user_key:
            return LookupResult.ABSENT, None
        if vtype == TYPE_VALUE:
            return LookupResult.FOUND, node_value
        return LookupResult.DELETED, None

    def iterator(self) -> MemTableIterator:
        return MemTableIterator(self._table.iterator())

    def entries(self):
        """Yield ``(internal_key, value)`` pairs in sorted order (for flush)."""
        yield from self._table


class MemTableIterator:
    """Adapts a skip-list cursor to the engine's internal-key iterator API."""

    def __init__(self, inner):
        self._inner = inner

    def valid(self) -> bool:
        return self._inner.valid()

    def internal_key(self) -> bytes:
        return self._inner.key()

    def key(self) -> bytes:
        """The user key at the current position."""
        return extract_user_key(self._inner.key())

    def value(self) -> bytes:
        return self._inner.value()

    def seek(self, internal_target: bytes) -> None:
        self._inner.seek(internal_target)

    def seek_to_first(self) -> None:
        self._inner.seek_to_first()

    def next(self) -> None:
        self._inner.next()


__all__ = ["MemTable", "MemTableIterator", "LookupResult"]
