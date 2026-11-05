"""The write-ahead log (WAL).

Durability starts here: before a batch touches the memtable it is appended to
the log, so a crash can never lose an acknowledged write — recovery replays the
log back into a fresh memtable.

Each record is framed as::

    crc    : fixed32   # masked CRC-32 of the payload
    length : fixed32   # payload length in bytes
    payload: length bytes

A crash can leave a *torn* final record (a partial header or a short payload).
Recovery treats a torn tail as the end of the log and drops it — that write was
never acknowledged. A CRC mismatch in the interior of the log is real
corruption: it stops recovery, and raises under ``paranoid_checks``.

(Strata uses simple length-prefixed framing rather than LevelDB's 32 KiB
block-fragmented log. It is easier to reason about and gives the same
crash-truncation guarantee; block fragmentation is on the roadmap.)
"""

from __future__ import annotations

import os

from .coding import decode_fixed32, encode_fixed32, masked_crc32
from .errors import Corruption

_HEADER_SIZE = 8  # fixed32 crc + fixed32 length

# Reject absurd record lengths early; a value this large almost certainly means
# a corrupt/garbage length field from a torn write.
MAX_RECORD_SIZE = 64 * 1024 * 1024


class LogWriter:
    """Appends length-prefixed, CRC-protected records to a file object."""

    def __init__(self, fileobj):
        self._file = fileobj

    def add_record(self, payload: bytes, sync: bool = False) -> None:
        if len(payload) > MAX_RECORD_SIZE:
            raise ValueError(f"log record too large: {len(payload)} bytes")
        header = encode_fixed32(masked_crc32(payload)) + encode_fixed32(len(payload))
        self._file.write(header)
        self._file.write(payload)
        self._file.flush()
        if sync:
            _fsync(self._file)

    def close(self) -> None:
        try:
            self._file.flush()
        finally:
            self._file.close()


class LogReader:
    """Iterates records written by :class:`LogWriter`.

    Yields each intact payload. Stops at a torn tail; on interior corruption it
    stops too, and raises :class:`Corruption` when ``paranoid`` is set.
    """

    def __init__(self, fileobj, paranoid: bool = False):
        self._file = fileobj
        self._paranoid = paranoid

    def __iter__(self):
        while True:
            header = self._file.read(_HEADER_SIZE)
            if len(header) < _HEADER_SIZE:
                return  # torn/absent header -> clean end of log
            expected_crc = decode_fixed32(header, 0)
            length = decode_fixed32(header, 4)
            if length > MAX_RECORD_SIZE:
                if self._paranoid:
                    raise Corruption(f"log record length {length} exceeds maximum")
                return
            payload = self._file.read(length)
            if len(payload) < length:
                return  # short payload -> torn tail
            if masked_crc32(payload) != expected_crc:
                if self._paranoid:
                    raise Corruption("log record failed CRC check")
                return
            yield payload


def _fsync(fileobj) -> None:
    """fsync a file object if it is backed by a real descriptor."""
    try:
        fileno = fileobj.fileno()
    except (AttributeError, OSError):
        return
    os.fsync(fileno)


__all__ = ["LogWriter", "LogReader", "MAX_RECORD_SIZE"]
