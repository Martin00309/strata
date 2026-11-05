"""Tests for minor (flush) and major (leveled) compaction."""

from __future__ import annotations

import os
from functools import cmp_to_key

from strata.compaction import flush_memtable, pick_compaction, run_compaction
from strata.filenames import table_file_name
from strata.internalkey import (
    TYPE_DELETION,
    TYPE_VALUE,
    compare_internal_keys,
    make_internal_key,
)
from strata.memtable import LookupResult, MemTable
from strata.options import Options
from strata.sstable import SSTableReader, SSTableWriter
from strata.table_cache import TableCache
from strata.version import FileMetaData, Version


def _make_table(dbname, opts, number, entries):
    """entries: (user, seq, type, value) -> write table, return FileMetaData."""
    internal = [(make_internal_key(u, s, t), v) for (u, s, t, v) in entries]
    internal.sort(key=lambda kv: cmp_to_key(compare_internal_keys)(kv[0]))
    with open(table_file_name(dbname, number), "wb") as f:
        w = SSTableWriter(f, opts)
        for ik, v in internal:
            w.add(ik, v)
        meta = w.finish()
    return FileMetaData(number, meta.file_size, meta.smallest, meta.largest)


def _counter(start):
    box = {"n": start}

    def nxt():
        box["n"] += 1
        return box["n"]

    return nxt


def test_flush_memtable_writes_all_versions(tmp_path):
    dbname = str(tmp_path)
    opts = Options()
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"a", b"a1")
    mt.add(2, TYPE_VALUE, b"b", b"b2")
    mt.add(3, TYPE_VALUE, b"a", b"a3")
    meta = flush_memtable(dbname, opts, 10, mt)
    reader = SSTableReader.open(table_file_name(dbname, 10), opts)
    try:
        assert reader.get(b"a", 10) == (LookupResult.FOUND, b"a3")
        assert reader.get(b"a", 2) == (LookupResult.FOUND, b"a1")
        assert reader.get(b"b", 10) == (LookupResult.FOUND, b"b2")
        assert meta.number == 10
    finally:
        reader.close()


def test_pick_compaction_none_when_balanced(tmp_path):
    opts = Options()
    v = Version(opts.num_levels)
    assert pick_compaction(v, opts) is None


def test_trivial_move(tmp_path):
    dbname = str(tmp_path)
    opts = Options(level0_file_num_compaction_trigger=1)
    f = _make_table(dbname, opts, 5, [(b"k", 1, TYPE_VALUE, b"v")])
    v = Version(opts.num_levels)
    v.files[1] = [f]  # single file at level 1, nothing at level 2
    from strata.compaction import Compaction

    comp = Compaction(1, [[f], []])
    assert comp.is_trivial_move()
    cache = TableCache(dbname, opts)
    try:
        edit = run_compaction(dbname, opts, v, cache, comp, smallest_snapshot=10, next_file_number=_counter(100))
        assert (1, 5) in edit.deleted_files
        assert [(lvl, m.number) for lvl, m in edit.new_files] == [(2, 5)]
    finally:
        cache.close()


def test_major_compaction_drops_superseded_versions(tmp_path):
    dbname = str(tmp_path)
    opts = Options()
    old = _make_table(dbname, opts, 1, [(b"k", 1, TYPE_VALUE, b"old")])
    new = _make_table(dbname, opts, 2, [(b"k", 5, TYPE_VALUE, b"new")])
    v = Version(opts.num_levels)
    v.files[0] = [old, new]
    from strata.compaction import Compaction

    comp = Compaction(0, [[old, new], []])
    cache = TableCache(dbname, opts)
    try:
        edit = run_compaction(dbname, opts, v, cache, comp, smallest_snapshot=10, next_file_number=_counter(100))
        assert len(edit.new_files) == 1
        out_number = edit.new_files[0][1].number
    finally:
        cache.close()
    reader = SSTableReader.open(table_file_name(dbname, out_number), opts)
    try:
        assert reader.get(b"k", 10) == (LookupResult.FOUND, b"new")
        # Only one surviving version.
        it = reader.iterator()
        it.seek_to_first()
        count = 0
        while it.valid():
            count += 1
            it.next()
        assert count == 1
    finally:
        reader.close()


def test_tombstone_dropped_at_base_level(tmp_path):
    dbname = str(tmp_path)
    opts = Options()
    val = _make_table(dbname, opts, 1, [(b"k", 1, TYPE_VALUE, b"v")])
    tomb = _make_table(dbname, opts, 2, [(b"k", 3, TYPE_DELETION, b"")])
    v = Version(opts.num_levels)
    v.files[0] = [val, tomb]  # no deeper levels => base level for the key
    from strata.compaction import Compaction

    comp = Compaction(0, [[val, tomb], []])
    cache = TableCache(dbname, opts)
    try:
        edit = run_compaction(dbname, opts, v, cache, comp, smallest_snapshot=10, next_file_number=_counter(100))
        # Both value and tombstone are obsolete -> no output file at all.
        assert edit.new_files == []
        assert {(0, 1), (0, 2)} <= edit.deleted_files
    finally:
        cache.close()


def test_tombstone_kept_when_not_base_level(tmp_path):
    dbname = str(tmp_path)
    opts = Options()
    tomb = _make_table(dbname, opts, 2, [(b"k", 3, TYPE_DELETION, b"")])
    deeper = _make_table(dbname, opts, 9, [(b"k", 1, TYPE_VALUE, b"v")])
    v = Version(opts.num_levels)
    v.files[0] = [tomb]
    v.files[2] = [deeper]  # key still present two levels down
    from strata.compaction import Compaction

    # Compact level 0 -> level 1; level 2 still holds the key, so the tombstone
    # must be preserved or the deeper value would resurrect.
    comp = Compaction(0, [[tomb], []])
    cache = TableCache(dbname, opts)
    try:
        edit = run_compaction(dbname, opts, v, cache, comp, smallest_snapshot=10, next_file_number=_counter(100))
        assert len(edit.new_files) == 1
        out_number = edit.new_files[0][1].number
    finally:
        cache.close()
    reader = SSTableReader.open(table_file_name(dbname, out_number), opts)
    try:
        assert reader.get(b"k", 10) == (LookupResult.DELETED, None)
    finally:
        reader.close()


def test_snapshot_protects_old_version(tmp_path):
    dbname = str(tmp_path)
    opts = Options()
    t = _make_table(
        dbname, opts, 1,
        [(b"k", 2, TYPE_VALUE, b"v2"), (b"k", 8, TYPE_VALUE, b"v8")],
    )
    v = Version(opts.num_levels)
    v.files[0] = [t]
    from strata.compaction import Compaction

    comp = Compaction(0, [[t], []])
    cache = TableCache(dbname, opts)
    try:
        # Oldest live snapshot is 5: the seq-2 version must be retained because
        # a reader at snapshot 5 would see it.
        edit = run_compaction(dbname, opts, v, cache, comp, smallest_snapshot=5, next_file_number=_counter(100))
        out_number = edit.new_files[0][1].number
    finally:
        cache.close()
    reader = SSTableReader.open(table_file_name(dbname, out_number), opts)
    try:
        assert reader.get(b"k", 8) == (LookupResult.FOUND, b"v8")
        assert reader.get(b"k", 4) == (LookupResult.FOUND, b"v2")
    finally:
        reader.close()


def test_output_split_by_max_file_size(tmp_path):
    dbname = str(tmp_path)
    opts = Options(max_file_size=4096, block_size=512)
    # Two overlapping level-0 files (even and odd keys) force a real rewrite
    # rather than a trivial move, so the output must split by size.
    evens = [(f"{i:06d}".encode(), 1, TYPE_VALUE, b"x" * 100) for i in range(0, 400, 2)]
    odds = [(f"{i:06d}".encode(), 1, TYPE_VALUE, b"x" * 100) for i in range(1, 400, 2)]
    a = _make_table(dbname, opts, 1, evens)
    b = _make_table(dbname, opts, 2, odds)
    v = Version(opts.num_levels)
    v.files[0] = [a, b]
    from strata.compaction import Compaction

    comp = Compaction(0, [[a, b], []])
    cache = TableCache(dbname, opts)
    try:
        edit = run_compaction(dbname, opts, v, cache, comp, smallest_snapshot=10, next_file_number=_counter(100))
        assert len(edit.new_files) > 1, "expected the output to split into multiple files"
        # Every key should survive the split, in order, across files.
        all_keys = []
        for _lvl, meta in edit.new_files:
            r = SSTableReader.open(table_file_name(dbname, meta.number), opts)
            it = r.iterator()
            it.seek_to_first()
            while it.valid():
                all_keys.append(it.key())
                it.next()
            r.close()
        assert all_keys == [f"{i:06d}".encode() for i in range(400)]
    finally:
        cache.close()


def test_fsync_paths_are_written(tmp_path):
    # Sanity: flushed files actually exist on disk.
    dbname = str(tmp_path)
    opts = Options()
    mt = MemTable()
    mt.add(1, TYPE_VALUE, b"a", b"1")
    flush_memtable(dbname, opts, 3, mt)
    assert os.path.exists(table_file_name(dbname, 3))
