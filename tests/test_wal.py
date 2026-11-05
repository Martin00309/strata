"""Tests for the write-ahead log, including crash-truncation recovery."""

from __future__ import annotations

import io

import pytest

from strata.coding import encode_fixed32, masked_crc32
from strata.errors import Corruption
from strata.wal import LogReader, LogWriter


def _write_records(records):
    buf = io.BytesIO()
    writer = LogWriter(buf)
    for r in records:
        writer.add_record(r)
    return buf.getvalue()


def test_roundtrip_many_records():
    records = [b"", b"a", b"hello world", bytes(range(256)) * 4]
    raw = _write_records(records)
    out = list(LogReader(io.BytesIO(raw)))
    assert out == records


def test_empty_log_yields_nothing():
    assert list(LogReader(io.BytesIO(b""))) == []


def test_torn_header_tail_is_dropped():
    raw = _write_records([b"first", b"second"])
    # Simulate a crash mid-way through writing a third record's header.
    truncated = raw + b"\x01\x02\x03"
    out = list(LogReader(io.BytesIO(truncated)))
    assert out == [b"first", b"second"]


def test_short_payload_tail_is_dropped():
    raw = _write_records([b"complete"])
    # A header promising 100 bytes, followed by only 4.
    header = encode_fixed32(masked_crc32(b"x" * 100)) + encode_fixed32(100)
    out = list(LogReader(io.BytesIO(raw + header + b"abcd")))
    assert out == [b"complete"]


def test_interior_crc_mismatch_stops_reading():
    good = _write_records([b"one"])
    # Build a corrupt record: valid length, wrong CRC, then a trailing record.
    payload = b"two"
    bad = encode_fixed32(masked_crc32(payload) ^ 0xFFFF) + encode_fixed32(len(payload)) + payload
    trailing = _write_records([b"three"])
    stream = good + bad + trailing
    # Non-paranoid: stop at the corrupt record, keep what came before.
    assert list(LogReader(io.BytesIO(stream))) == [b"one"]


def test_paranoid_crc_mismatch_raises():
    payload = b"two"
    bad = encode_fixed32(masked_crc32(payload) ^ 0xFFFF) + encode_fixed32(len(payload)) + payload
    with pytest.raises(Corruption):
        list(LogReader(io.BytesIO(bad), paranoid=True))


def test_rejects_oversize_record_on_write():
    from strata.wal import MAX_RECORD_SIZE

    writer = LogWriter(io.BytesIO())
    with pytest.raises(ValueError):
        writer.add_record(b"x" * (MAX_RECORD_SIZE + 1))


def test_file_backed_roundtrip(tmp_path):
    path = tmp_path / "000001.log"
    with open(path, "wb") as f:
        writer = LogWriter(f)
        writer.add_record(b"durable", sync=True)
        writer.add_record(b"record")
    with open(path, "rb") as f:
        assert list(LogReader(f)) == [b"durable", b"record"]
