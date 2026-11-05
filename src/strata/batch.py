"""Write batches — the atomic unit of mutation.

Every mutation Strata applies, even a single ``put``, travels as a
:class:`WriteBatch`: a flat, self-describing byte buffer that is appended to the
write-ahead log verbatim and then replayed into the memtable. Grouping writes
into a batch gives two things for free:

* **atomicity** — the whole batch is assigned a contiguous run of sequence
  numbers and logged as one record, so recovery never applies half of it; and
* **throughput** — one log append (and one optional fsync) covers many writes.

Wire format::

    sequence : fixed64   # sequence assigned to the batch's first operation
    count    : fixed32   # number of operations
    ops      : count × ( type:u8 , key:len-prefixed , [value:len-prefixed] )

where ``type`` is ``TYPE_VALUE`` (key + value) or ``TYPE_DELETION`` (key only).
"""

from __future__ import annotations

from .coding import (
    decode_fixed32,
    decode_fixed64,
    get_length_prefixed,
    put_fixed32,
    put_fixed64,
    put_length_prefixed,
)
from .errors import Corruption
from .internalkey import TYPE_DELETION, TYPE_VALUE

_HEADER = 12  # 8-byte sequence + 4-byte count


class WriteBatch:
    """An ordered list of put/delete operations applied atomically."""

    def __init__(self):
        self._rep = bytearray(_HEADER)  # zeroed header; body appended after

    # -- building ---------------------------------------------------------- #
    def put(self, key: bytes, value: bytes) -> WriteBatch:
        if not isinstance(key, (bytes, bytearray)):
            raise TypeError("key must be bytes")
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("value must be bytes")
        self._set_count(self.count + 1)
        self._rep.append(TYPE_VALUE)
        put_length_prefixed(self._rep, bytes(key))
        put_length_prefixed(self._rep, bytes(value))
        return self

    def delete(self, key: bytes) -> WriteBatch:
        if not isinstance(key, (bytes, bytearray)):
            raise TypeError("key must be bytes")
        self._set_count(self.count + 1)
        self._rep.append(TYPE_DELETION)
        put_length_prefixed(self._rep, bytes(key))
        return self

    def append(self, other: WriteBatch) -> None:
        """Merge ``other``'s operations onto the end of this batch."""
        self._set_count(self.count + other.count)
        self._rep += other._rep[_HEADER:]

    def clear(self) -> None:
        self._rep = bytearray(_HEADER)

    # -- header accessors -------------------------------------------------- #
    @property
    def count(self) -> int:
        return decode_fixed32(self._rep, 8)

    def _set_count(self, value: int) -> None:
        self._rep[8:12] = bytearray(4)
        put_fixed32_into(self._rep, 8, value)

    @property
    def sequence(self) -> int:
        return decode_fixed64(self._rep, 0)

    @sequence.setter
    def sequence(self, value: int) -> None:
        self._rep[0:8] = bytearray(8)
        put_fixed64_into(self._rep, 0, value)

    def __len__(self) -> int:
        return self.count

    def approximate_size(self) -> int:
        return len(self._rep)

    # -- serialisation ----------------------------------------------------- #
    def encode(self) -> bytes:
        return bytes(self._rep)

    @classmethod
    def from_bytes(cls, data: bytes) -> WriteBatch:
        if len(data) < _HEADER:
            raise Corruption("write batch shorter than its header")
        batch = cls()
        batch._rep = bytearray(data)
        # Validate the body parses cleanly and matches the declared count.
        parsed = sum(1 for _ in batch._iter_ops())
        if parsed != batch.count:
            raise Corruption(
                f"write batch count mismatch: header says {batch.count}, body has {parsed}"
            )
        return batch

    # -- iteration --------------------------------------------------------- #
    def _iter_ops(self):
        """Yield ``(value_type, key, value|None)`` for each operation."""
        data = self._rep
        offset = _HEADER
        n = len(data)
        while offset < n:
            vtype = data[offset]
            offset += 1
            key, offset = get_length_prefixed(data, offset)
            if vtype == TYPE_VALUE:
                value, offset = get_length_prefixed(data, offset)
                yield vtype, key, value
            elif vtype == TYPE_DELETION:
                yield vtype, key, None
            else:
                raise Corruption(f"unknown write-batch op type {vtype}")

    def iterate(self, handler) -> None:
        """Replay the batch into ``handler``.

        ``handler`` must expose ``put(sequence, key, value)`` and
        ``delete(sequence, key)``. Each operation is handed the sequence number
        it was assigned (``batch.sequence + i``).
        """
        seq = self.sequence
        for vtype, key, value in self._iter_ops():
            if vtype == TYPE_VALUE:
                handler.put(seq, key, value)
            else:
                handler.delete(seq, key)
            seq += 1


def put_fixed32_into(buf: bytearray, offset: int, value: int) -> None:
    chunk = bytearray()
    put_fixed32(chunk, value)
    buf[offset : offset + 4] = chunk


def put_fixed64_into(buf: bytearray, offset: int, value: int) -> None:
    chunk = bytearray()
    put_fixed64(chunk, value)
    buf[offset : offset + 8] = chunk


class _MemTableInserter:
    """Adapter that applies batch operations to a memtable."""

    __slots__ = ("memtable",)

    def __init__(self, memtable):
        self.memtable = memtable

    def put(self, sequence: int, key: bytes, value: bytes) -> None:
        self.memtable.add(sequence, TYPE_VALUE, key, value)

    def delete(self, sequence: int, key: bytes) -> None:
        self.memtable.add(sequence, TYPE_DELETION, key, b"")


def insert_into_memtable(batch: WriteBatch, memtable) -> None:
    """Apply every operation in ``batch`` to ``memtable``."""
    batch.iterate(_MemTableInserter(memtable))


__all__ = ["WriteBatch", "insert_into_memtable"]
