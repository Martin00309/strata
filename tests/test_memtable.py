"""Tests for the MVCC memtable."""

from __future__ import annotations

from strata.internalkey import TYPE_DELETION, TYPE_VALUE, parse_internal_key
from strata.memtable import LookupResult, MemTable


def test_put_and_get_latest():
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"k", b"v1")
    mt.add(2, TYPE_VALUE, b"k", b"v2")
    status, value = mt.get(b"k", sequence=2)
    assert status == LookupResult.FOUND
    assert value == b"v2"


def test_snapshot_reads_see_older_version():
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"k", b"v1")
    mt.add(5, TYPE_VALUE, b"k", b"v2")
    # As of sequence 3, only the seq-1 write is visible.
    status, value = mt.get(b"k", sequence=3)
    assert status == LookupResult.FOUND
    assert value == b"v1"


def test_read_before_any_write_is_absent():
    mt = MemTable()
    mt.add(10, TYPE_VALUE, b"k", b"v")
    status, _ = mt.get(b"k", sequence=9)
    assert status == LookupResult.ABSENT


def test_deletion_tombstone_shadows_value():
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"k", b"v1")
    mt.add(2, TYPE_DELETION, b"k", b"")
    status, value = mt.get(b"k", sequence=2)
    assert status == LookupResult.DELETED
    assert value is None
    # But a snapshot before the delete still sees the value.
    status, value = mt.get(b"k", sequence=1)
    assert status == LookupResult.FOUND
    assert value == b"v1"


def test_absent_key():
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"present", b"x")
    status, _ = mt.get(b"absent", sequence=99)
    assert status == LookupResult.ABSENT


def test_entries_are_sorted_newest_first_within_key():
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"b", b"b1")
    mt.add(3, TYPE_VALUE, b"a", b"a3")
    mt.add(2, TYPE_VALUE, b"a", b"a2")
    order = [parse_internal_key(ik)[:2] for ik, _ in mt.entries()]
    assert order == [(b"a", 3), (b"a", 2), (b"b", 1)]


def test_memory_usage_grows():
    mt = MemTable()
    assert mt.approximate_memory_usage() == 0
    mt.add(1, TYPE_VALUE, b"k", b"value")
    assert mt.approximate_memory_usage() > 0


def test_iterator_walks_user_keys():
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"a", b"1")
    mt.add(2, TYPE_VALUE, b"c", b"3")
    mt.add(3, TYPE_VALUE, b"b", b"2")
    it = mt.iterator()
    it.seek_to_first()
    seen = []
    while it.valid():
        seen.append((it.key(), it.value()))
        it.next()
    assert seen == [(b"a", b"1"), (b"b", b"2"), (b"c", b"3")]
