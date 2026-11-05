"""Tests for the write batch."""

from __future__ import annotations

import pytest

from strata.batch import WriteBatch, insert_into_memtable
from strata.errors import Corruption
from strata.memtable import LookupResult, MemTable


class _Recorder:
    def __init__(self):
        self.ops = []

    def put(self, sequence, key, value):
        self.ops.append(("put", sequence, key, value))

    def delete(self, sequence, key):
        self.ops.append(("del", sequence, key))


def test_put_and_delete_count():
    b = WriteBatch()
    assert len(b) == 0
    b.put(b"a", b"1")
    b.put(b"b", b"2")
    b.delete(b"c")
    assert len(b) == 3


def test_iterate_assigns_sequential_sequence_numbers():
    b = WriteBatch()
    b.put(b"a", b"1")
    b.delete(b"b")
    b.put(b"c", b"3")
    b.sequence = 100
    rec = _Recorder()
    b.iterate(rec)
    assert rec.ops == [
        ("put", 100, b"a", b"1"),
        ("del", 101, b"b"),
        ("put", 102, b"c", b"3"),
    ]


def test_encode_decode_roundtrip():
    b = WriteBatch()
    b.put(b"key", b"value")
    b.delete(b"gone")
    b.sequence = 42
    raw = b.encode()
    restored = WriteBatch.from_bytes(raw)
    assert restored.sequence == 42
    assert len(restored) == 2
    rec = _Recorder()
    restored.iterate(rec)
    assert rec.ops == [("put", 42, b"key", b"value"), ("del", 43, b"gone")]


def test_append_merges_batches():
    a = WriteBatch()
    a.put(b"a", b"1")
    other = WriteBatch()
    other.put(b"b", b"2")
    other.delete(b"c")
    a.append(other)
    assert len(a) == 3


def test_clear_resets():
    b = WriteBatch()
    b.put(b"a", b"1")
    b.clear()
    assert len(b) == 0


def test_from_bytes_rejects_short_buffer():
    with pytest.raises(Corruption):
        WriteBatch.from_bytes(b"\x00\x01")


def test_from_bytes_rejects_count_mismatch():
    b = WriteBatch()
    b.put(b"a", b"1")
    raw = bytearray(b.encode())
    raw[8] = 5  # claim 5 ops, body has 1
    with pytest.raises(Corruption):
        WriteBatch.from_bytes(bytes(raw))


def test_insert_into_memtable_applies_ops():
    b = WriteBatch()
    b.put(b"k", b"v")
    b.delete(b"d")
    b.sequence = 7
    mt = MemTable()
    insert_into_memtable(b, mt)
    assert mt.get(b"k", 7) == (LookupResult.FOUND, b"v")
    assert mt.get(b"d", 8) == (LookupResult.DELETED, None)


def test_rejects_non_bytes():
    b = WriteBatch()
    with pytest.raises(TypeError):
        b.put("not-bytes", b"v")
    with pytest.raises(TypeError):
        b.put(b"k", "not-bytes")
