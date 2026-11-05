"""Blocks — the sorted, prefix-compressed unit inside a table file.

A table is a sequence of blocks (data blocks plus one index block). Within a
block, entries are stored in comparator order with *prefix compression*: each
key records only the bytes it does not share with its predecessor. To keep the
block binary-searchable despite compression, a full key is stored every
``restart_interval`` entries at a *restart point*, and the offsets of those
points are listed at the tail::

    entries ... | restart[0] | restart[1] | ... | num_restarts
                   (each restart offset and the count are fixed32)

Each entry is::

    shared:varint  non_shared:varint  value_len:varint  key_delta  value

A seek binary-searches the restart points, then scans forward from the chosen
restart decompressing keys until it reaches the target.
"""

from __future__ import annotations

from collections.abc import Callable

from .coding import decode_fixed32, get_varint, put_fixed32, put_varint


def _shared_prefix_len(a: bytes, b: bytes) -> int:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


class BlockBuilder:
    def __init__(self, restart_interval: int):
        self._restart_interval = restart_interval
        self._buffer = bytearray()
        self._restarts = [0]
        self._counter = 0
        self._last_key = b""
        self._finished = False

    def reset(self) -> None:
        self._buffer = bytearray()
        self._restarts = [0]
        self._counter = 0
        self._last_key = b""
        self._finished = False

    def empty(self) -> bool:
        return len(self._buffer) == 0

    def current_size_estimate(self) -> int:
        return len(self._buffer) + len(self._restarts) * 4 + 4

    def add(self, key: bytes, value: bytes) -> None:
        assert not self._finished, "add() after finish()"
        if self._counter >= self._restart_interval:
            # Start a new restart: store the key in full.
            self._restarts.append(len(self._buffer))
            self._counter = 0
            shared = 0
        else:
            shared = _shared_prefix_len(self._last_key, key)
        non_shared = len(key) - shared

        put_varint(self._buffer, shared)
        put_varint(self._buffer, non_shared)
        put_varint(self._buffer, len(value))
        self._buffer += key[shared:]
        self._buffer += value

        self._last_key = bytes(key)
        self._counter += 1

    def finish(self) -> bytes:
        for offset in self._restarts:
            put_fixed32(self._buffer, offset)
        put_fixed32(self._buffer, len(self._restarts))
        self._finished = True
        return bytes(self._buffer)


class Block:
    """A read-only view over a finished block's bytes."""

    def __init__(self, data: bytes, comparator: Callable[[bytes, bytes], int]):
        if len(data) < 4:
            raise ValueError("block smaller than its restart trailer")
        self._data = data
        self._cmp = comparator
        self._num_restarts = decode_fixed32(data, len(data) - 4)
        self._restart_offset = len(data) - 4 - self._num_restarts * 4
        if self._restart_offset < 0:
            raise ValueError("corrupt block: restart array overruns block")

    def iterator(self) -> BlockIterator:
        return BlockIterator(
            self._data, self._cmp, self._restart_offset, self._num_restarts
        )

    def __iter__(self):
        it = self.iterator()
        it.seek_to_first()
        while it.valid():
            yield it.key(), it.value()
            it.next()


class BlockIterator:
    """A seekable forward cursor over a block.

    The position is two offsets into the entries region: ``_current`` (start of
    the current entry) and ``_next`` (start of the one after). The iterator is
    invalid when ``_current`` reaches the restart array.
    """

    def __init__(self, data, comparator, restart_offset, num_restarts):
        self._data = data
        self._cmp = comparator
        self._restart_offset = restart_offset
        self._num_restarts = num_restarts
        self._current = restart_offset  # invalid position
        self._next = restart_offset
        self._key = b""
        self._value = b""

    def valid(self) -> bool:
        return self._current < self._restart_offset

    def key(self) -> bytes:
        assert self.valid(), "key() on invalid iterator"
        return self._key

    def value(self) -> bytes:
        assert self.valid(), "value() on invalid iterator"
        return self._value

    def _restart_point(self, index: int) -> int:
        return decode_fixed32(self._data, self._restart_offset + index * 4)

    def _decode_at(self, offset: int, last_key: bytes):
        """Decode one entry at ``offset``; return ``(key, value, next_offset)``."""
        shared, pos = get_varint(self._data, offset)
        non_shared, pos = get_varint(self._data, pos)
        value_len, pos = get_varint(self._data, pos)
        key = last_key[:shared] + self._data[pos : pos + non_shared]
        pos += non_shared
        value = self._data[pos : pos + value_len]
        pos += value_len
        return bytes(key), bytes(value), pos

    def _set_current(self, offset: int, last_key: bytes) -> None:
        if offset >= self._restart_offset:
            self._current = self._restart_offset
            return
        self._key, self._value, self._next = self._decode_at(offset, last_key)
        self._current = offset

    def seek_to_first(self) -> None:
        self._set_current(0, b"")

    def next(self) -> None:
        assert self.valid(), "next() on invalid iterator"
        self._set_current(self._next, self._key)

    def seek(self, target: bytes) -> None:
        if self._num_restarts == 0:
            self._current = self._restart_offset
            return
        # Binary search the restart points for the last whose first key <= target.
        left, right = 0, self._num_restarts - 1
        while left < right:
            mid = (left + right + 1) // 2
            key, _, _ = self._decode_at(self._restart_point(mid), b"")
            if self._cmp(key, target) < 0:
                left = mid
            else:
                right = mid - 1

        # Linear scan forward from restart ``left`` until key >= target.
        offset = self._restart_point(left)
        last_key = b""
        while offset < self._restart_offset:
            key, value, nxt = self._decode_at(offset, last_key)
            if self._cmp(key, target) >= 0:
                self._key, self._value, self._next = key, value, nxt
                self._current = offset
                return
            last_key = key
            offset = nxt
        self._current = self._restart_offset  # target past the last key


__all__ = ["BlockBuilder", "Block", "BlockIterator"]
