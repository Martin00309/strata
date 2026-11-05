"""Tests for the merging iterator and snapshot-aware DB iterator."""

from __future__ import annotations

from functools import cmp_to_key

from strata.internalkey import (
    TYPE_DELETION,
    TYPE_VALUE,
    compare_internal_keys,
    make_internal_key,
    parse_internal_key,
)
from strata.iterator import DBIterator, MergingIterator
from strata.memtable import MemTable
from strata.options import Options
from strata.sstable import SSTableReader, SSTableWriter


def _table(path, entries, options=None):
    options = options or Options()
    internal = [(make_internal_key(u, s, t), v) for (u, s, t, v) in entries]
    internal.sort(key=lambda kv: cmp_to_key(compare_internal_keys)(kv[0]))
    with open(path, "wb") as f:
        w = SSTableWriter(f, options)
        for ik, v in internal:
            w.add(ik, v)
        w.finish()
    return SSTableReader.open(str(path), options)


def test_merging_sorts_across_memtables():
    a = MemTable()
    a.add(1, TYPE_VALUE, b"a", b"A")
    a.add(3, TYPE_VALUE, b"c", b"C")
    b = MemTable()
    b.add(2, TYPE_VALUE, b"b", b"B")
    b.add(4, TYPE_VALUE, b"d", b"D")
    merged = MergingIterator([a.iterator(), b.iterator()])
    merged.seek_to_first()
    users = []
    while merged.valid():
        users.append(parse_internal_key(merged.internal_key())[0])
        merged.next()
    assert users == [b"a", b"b", b"c", b"d"]


def test_dbiterator_keeps_latest_version():
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"a", b"a1")
    mt.add(3, TYPE_VALUE, b"a", b"a3")
    mt.add(2, TYPE_VALUE, b"b", b"b2")
    merged = MergingIterator([mt.iterator()])
    assert list(DBIterator(merged, snapshot=10)) == [(b"a", b"a3"), (b"b", b"b2")]


def test_dbiterator_snapshot_sees_older_version():
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"a", b"a1")
    mt.add(5, TYPE_VALUE, b"a", b"a5")
    merged = MergingIterator([mt.iterator()])
    assert list(DBIterator(merged, snapshot=3)) == [(b"a", b"a1")]


def test_dbiterator_hides_tombstone():
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"a", b"a1")
    mt.add(3, TYPE_DELETION, b"a", b"")
    mt.add(2, TYPE_VALUE, b"b", b"b2")
    merged = MergingIterator([mt.iterator()])
    # As of 10, "a" is deleted; only "b" remains.
    assert list(DBIterator(merged, snapshot=10)) == [(b"b", b"b2")]
    # As of 1, the delete has not happened yet.
    merged2 = MergingIterator([mt.iterator()])
    assert list(DBIterator(merged2, snapshot=1)) == [(b"a", b"a1")]


def test_dbiterator_range_is_half_open():
    mt = MemTable()
    for i, k in enumerate([b"a", b"b", b"c", b"d", b"e"], start=1):
        mt.add(i, TYPE_VALUE, k, k.upper())
    merged = MergingIterator([mt.iterator()])
    rows = list(DBIterator(merged, snapshot=99, start=b"b", end=b"d"))
    assert rows == [(b"b", b"B"), (b"c", b"C")]


def test_newest_source_wins_across_memtable_and_table(tmp_path):
    reader = _table(tmp_path / "t.sst", [(b"k", 1, TYPE_VALUE, b"old")])
    try:
        mt = MemTable()
        mt.add(5, TYPE_VALUE, b"k", b"new")
        merged = MergingIterator([mt.iterator(), reader.iterator()])
        assert list(DBIterator(merged, snapshot=10)) == [(b"k", b"new")]
        # A snapshot before the memtable write sees the table's version.
        merged2 = MergingIterator([mt.iterator(), reader.iterator()])
        assert list(DBIterator(merged2, snapshot=1)) == [(b"k", b"old")]
    finally:
        reader.close()


def test_tombstone_in_memtable_hides_table_value(tmp_path):
    reader = _table(tmp_path / "t.sst", [(b"k", 1, TYPE_VALUE, b"v")])
    try:
        mt = MemTable()
        mt.add(5, TYPE_DELETION, b"k", b"")
        merged = MergingIterator([mt.iterator(), reader.iterator()])
        assert list(DBIterator(merged, snapshot=10)) == []
    finally:
        reader.close()


def test_empty_merge():
    merged = MergingIterator([])
    assert list(DBIterator(merged, snapshot=1)) == []
