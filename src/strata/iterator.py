"""Iterators that stitch the memtable and on-disk tables into one view.

A scan has to merge several sorted sources — the active memtable, a memtable
being flushed, and every SSTable across every level — into a single ordered
stream, then collapse that stream of *versioned* internal keys into the
user-visible key/value pairs at a chosen snapshot.

:class:`MergingIterator` does the first half (an N-way merge over the internal
keys). :class:`DBIterator` does the second: for each user key it keeps the
newest version not newer than the snapshot, emits it if it is a value, and
drops it if it is a deletion tombstone.
"""

from __future__ import annotations

from .internalkey import (
    MAX_SEQUENCE,
    TYPE_VALUE,
    compare_internal_keys,
    make_internal_key,
    parse_internal_key,
)

# Trailer type that makes an internal key sort before every real version of the
# same user key (so a seek lands on the newest version).
_TYPE_SEEK = 0xFF


class MergingIterator:
    """An N-way merge over child iterators keyed by internal key.

    Each child exposes ``valid`` / ``internal_key`` / ``value`` / ``seek`` /
    ``seek_to_first`` / ``next``. Internal keys are globally unique (sequence
    numbers never repeat), so the merge needs no tie-breaking.

    The merge picks the smallest key by a linear scan of the children, which is
    the right trade-off for the handful of sources a scan touches; a heap-based
    merge for very wide fan-in is on the roadmap.
    """

    def __init__(self, children: list):
        self._children = children
        self._current = None

    def valid(self) -> bool:
        return self._current is not None

    def internal_key(self) -> bytes:
        return self._current.internal_key()

    def value(self) -> bytes:
        return self._current.value()

    def seek_to_first(self) -> None:
        for child in self._children:
            child.seek_to_first()
        self._find_smallest()

    def seek(self, internal_target: bytes) -> None:
        for child in self._children:
            child.seek(internal_target)
        self._find_smallest()

    def next(self) -> None:
        assert self._current is not None, "next() on invalid iterator"
        self._current.next()
        self._find_smallest()

    def _find_smallest(self) -> None:
        smallest = None
        for child in self._children:
            if not child.valid():
                continue
            if smallest is None:
                smallest = child
            elif compare_internal_keys(child.internal_key(), smallest.internal_key()) < 0:
                smallest = child
        self._current = smallest


class DBIterator:
    """Snapshot-consistent forward iteration over user key/value pairs.

    Yields ``(user_key, value)`` for keys in ``[start, end)`` (both optional;
    ``start`` is inclusive, ``end`` exclusive), skipping keys that are deleted
    or not yet written as of ``snapshot``.
    """

    def __init__(self, merged: MergingIterator, snapshot: int, start=None, end=None):
        self._merged = merged
        self._snapshot = snapshot
        self._start = start
        self._end = end

    def __iter__(self):
        it = self._merged
        if self._start is None:
            it.seek_to_first()
        else:
            # Smallest internal key for ``start`` — positions before every
            # version of it so visibility is decided below, not by the seek.
            it.seek(make_internal_key(self._start, MAX_SEQUENCE, _TYPE_SEEK))

        while it.valid():
            user_key, _seq, _vtype = parse_internal_key(it.internal_key())
            if self._end is not None and user_key >= self._end:
                return

            chosen_type = None
            chosen_value = None
            # Walk every version of this user key (newest first) and keep the
            # first one visible at the snapshot.
            while it.valid():
                u, seq, vtype = parse_internal_key(it.internal_key())
                if u != user_key:
                    break
                if chosen_type is None and seq <= self._snapshot:
                    chosen_type = vtype
                    chosen_value = it.value()
                it.next()

            if chosen_type == TYPE_VALUE:
                yield user_key, chosen_value


__all__ = ["MergingIterator", "DBIterator"]
