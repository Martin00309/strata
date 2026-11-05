"""Tests for the SSTable writer and reader."""

from __future__ import annotations

from functools import cmp_to_key

import pytest

from strata.errors import Corruption
from strata.internalkey import (
    TYPE_DELETION,
    TYPE_VALUE,
    compare_internal_keys,
    make_internal_key,
)
from strata.memtable import LookupResult
from strata.options import Options
from strata.sstable import SSTableReader, SSTableWriter


def _write_table(path, entries, options=None):
    """entries: list of (user_key, sequence, value_type, value)."""
    options = options or Options()
    internal = [
        (make_internal_key(u, s, t), v) for (u, s, t, v) in entries
    ]
    internal.sort(key=lambda kv: cmp_to_key(compare_internal_keys)(kv[0]))
    with open(path, "wb") as f:
        writer = SSTableWriter(f, options)
        for ik, value in internal:
            writer.add(ik, value)
        meta = writer.finish()
    return meta


def test_roundtrip_point_lookups(tmp_path):
    path = tmp_path / "t.sst"
    entries = [(f"key{i:04d}".encode(), 1, TYPE_VALUE, f"val{i}".encode()) for i in range(200)]
    _write_table(path, entries)
    reader = SSTableReader.open(str(path), Options())
    try:
        for i in range(200):
            status, value = reader.get(f"key{i:04d}".encode(), sequence=10)
            assert status == LookupResult.FOUND
            assert value == f"val{i}".encode()
        # Absent keys.
        assert reader.get(b"nope", 10)[0] == LookupResult.ABSENT
        assert reader.get(b"key9999", 10)[0] == LookupResult.ABSENT
    finally:
        reader.close()


def test_multiple_blocks(tmp_path):
    # Tiny blocks force many data blocks and a multi-entry index.
    path = tmp_path / "t.sst"
    opts = Options(block_size=128)
    entries = [(f"{i:06d}".encode(), 1, TYPE_VALUE, (b"x" * 40)) for i in range(500)]
    meta = _write_table(path, entries, opts)
    assert meta.num_entries == 500
    reader = SSTableReader.open(str(path), opts)
    try:
        assert reader.get(b"000000", 5)[1] == b"x" * 40
        assert reader.get(b"000499", 5)[1] == b"x" * 40
        assert reader.get(b"000250", 5)[1] == b"x" * 40
    finally:
        reader.close()


def test_mvcc_get_respects_sequence(tmp_path):
    path = tmp_path / "t.sst"
    entries = [
        (b"k", 2, TYPE_VALUE, b"v2"),
        (b"k", 5, TYPE_VALUE, b"v5"),
        (b"k", 9, TYPE_VALUE, b"v9"),
    ]
    _write_table(path, entries)
    reader = SSTableReader.open(str(path), Options())
    try:
        assert reader.get(b"k", 9) == (LookupResult.FOUND, b"v9")
        assert reader.get(b"k", 6) == (LookupResult.FOUND, b"v5")
        assert reader.get(b"k", 2) == (LookupResult.FOUND, b"v2")
        assert reader.get(b"k", 1)[0] == LookupResult.ABSENT
    finally:
        reader.close()


def test_deletion_type_reported(tmp_path):
    path = tmp_path / "t.sst"
    entries = [
        (b"k", 1, TYPE_VALUE, b"v"),
        (b"k", 3, TYPE_DELETION, b""),
    ]
    _write_table(path, entries)
    reader = SSTableReader.open(str(path), Options())
    try:
        assert reader.get(b"k", 3) == (LookupResult.DELETED, None)
        assert reader.get(b"k", 1) == (LookupResult.FOUND, b"v")
    finally:
        reader.close()


def test_full_iteration_in_order(tmp_path):
    path = tmp_path / "t.sst"
    opts = Options(block_size=256)
    entries = [(f"{i:05d}".encode(), 7, TYPE_VALUE, str(i).encode()) for i in range(300)]
    _write_table(path, entries, opts)
    reader = SSTableReader.open(str(path), opts)
    try:
        it = reader.iterator()
        it.seek_to_first()
        keys = []
        while it.valid():
            keys.append(it.key())
            it.next()
        assert keys == [f"{i:05d}".encode() for i in range(300)]
    finally:
        reader.close()


def test_iterator_seek(tmp_path):
    path = tmp_path / "t.sst"
    opts = Options(block_size=256)
    entries = [(f"{i:05d}".encode(), 1, TYPE_VALUE, b"v") for i in range(0, 200, 2)]
    _write_table(path, entries, opts)
    reader = SSTableReader.open(str(path), opts)
    try:
        from strata.internalkey import make_lookup_key

        it = reader.iterator()
        it.seek(make_lookup_key(b"00100", 100))
        assert it.valid() and it.key() == b"00100"
        it.seek(make_lookup_key(b"00101", 100))  # lands on next even key
        assert it.valid() and it.key() == b"00102"
    finally:
        reader.close()


def test_bloom_filter_skips_absent(tmp_path):
    path = tmp_path / "t.sst"
    entries = [(f"present{i}".encode(), 1, TYPE_VALUE, b"v") for i in range(100)]
    _write_table(path, entries, Options(bloom_bits_per_key=10))
    reader = SSTableReader.open(str(path), Options(bloom_bits_per_key=10))
    try:
        assert reader.get(b"present50", 5)[0] == LookupResult.FOUND
        assert reader.get(b"definitely-absent", 5)[0] == LookupResult.ABSENT
    finally:
        reader.close()


def test_crc_corruption_detected(tmp_path):
    path = tmp_path / "t.sst"
    entries = [(f"k{i:03d}".encode(), 1, TYPE_VALUE, b"value") for i in range(50)]
    _write_table(path, entries, Options(block_size=128))
    # Corrupt a byte near the start (inside the first data block).
    data = bytearray(path.read_bytes())
    data[10] ^= 0xFF
    path.write_bytes(bytes(data))
    reader = SSTableReader.open(str(path), Options(block_size=128), read_verify=True)
    try:
        with pytest.raises(Corruption):
            reader.get(b"k000", 5)
    finally:
        reader.close()


def test_bad_magic_rejected(tmp_path):
    path = tmp_path / "t.sst"
    path.write_bytes(b"not a table at all, way too short-ish...." + b"\x00" * 40)
    with pytest.raises(Corruption):
        SSTableReader.open(str(path), Options())


def test_meta_tracks_key_range(tmp_path):
    path = tmp_path / "t.sst"
    entries = [(f"{i:03d}".encode(), 1, TYPE_VALUE, b"v") for i in range(10)]
    meta = _write_table(path, entries)
    from strata.internalkey import extract_user_key

    assert extract_user_key(meta.smallest) == b"000"
    assert extract_user_key(meta.largest) == b"009"
    assert meta.num_entries == 10
    assert meta.file_size > 0
