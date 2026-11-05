"""Tests for version edits, versions, the manifest, and the table cache."""

from __future__ import annotations

import os
from functools import cmp_to_key

from strata.internalkey import (
    TYPE_VALUE,
    compare_internal_keys,
    make_internal_key,
)
from strata.memtable import LookupResult
from strata.options import Options
from strata.sstable import SSTableWriter
from strata.table_cache import TableCache
from strata.version import (
    FileMetaData,
    Version,
    VersionEdit,
    VersionSet,
    max_bytes_for_level,
)


def _meta(number, lo, hi, size=1000):
    return FileMetaData(
        number,
        size,
        make_internal_key(lo, 100, TYPE_VALUE),
        make_internal_key(hi, 100, TYPE_VALUE),
    )


def test_version_edit_roundtrip():
    edit = VersionEdit()
    edit.comparator_name = "strata.InternalKeyComparator"
    edit.log_number = 7
    edit.next_file_number = 9
    edit.last_sequence = 42
    edit.add_file(0, _meta(5, b"a", b"m"))
    edit.add_file(1, _meta(6, b"n", b"z", size=2048))
    edit.delete_file(2, 3)

    decoded = VersionEdit.decode(edit.encode())
    assert decoded.comparator_name == edit.comparator_name
    assert decoded.log_number == 7
    assert decoded.next_file_number == 9
    assert decoded.last_sequence == 42
    assert decoded.deleted_files == {(2, 3)}
    assert len(decoded.new_files) == 2
    (lvl0, m0), (lvl1, m1) = decoded.new_files
    assert lvl0 == 0 and m0.number == 5
    assert lvl1 == 1 and m1.number == 6 and m1.file_size == 2048


def test_find_file_binary_search():
    files = [_meta(1, b"a", b"c"), _meta(2, b"e", b"g"), _meta(3, b"m", b"p")]
    v = Version(7)
    v.files[1] = files
    # overlapping_inputs is the public shape of the range search.
    assert [f.number for f in v.overlapping_inputs(1, b"f", b"f")] == [2]
    assert [f.number for f in v.overlapping_inputs(1, b"a", b"z")] == [1, 2, 3]
    assert v.overlapping_inputs(1, b"h", b"l") == []  # falls in a gap


def test_max_bytes_for_level_grows_geometrically():
    opts = Options(max_bytes_for_level_base=10, max_bytes_for_level_multiplier=10)
    assert max_bytes_for_level(opts, 1) == 10
    assert max_bytes_for_level(opts, 2) == 100
    assert max_bytes_for_level(opts, 3) == 1000


def test_pick_compaction_level_by_file_count():
    opts = Options(level0_file_num_compaction_trigger=4)
    v = Version(opts.num_levels)
    v.files[0] = [_meta(i, b"a", b"b") for i in range(4)]
    picked = v.pick_compaction_level(opts)
    assert picked is not None and picked[0] == 0


def test_no_compaction_when_small():
    opts = Options()
    v = Version(opts.num_levels)
    v.files[0] = [_meta(1, b"a", b"b")]
    assert v.pick_compaction_level(opts) is None


def test_versionset_manifest_recovery(tmp_path):
    dbname = str(tmp_path / "db")
    os.makedirs(dbname)
    opts = Options()

    vs = VersionSet(dbname, opts)
    vs.log_number = 10
    vs.last_sequence = 55
    vs.new_file_number()  # consume a number as a WAL would
    vs.create_new_manifest()

    edit = VersionEdit()
    edit.add_file(0, _meta(vs.new_file_number(), b"a", b"f"))
    edit.add_file(1, _meta(vs.new_file_number(), b"g", b"z"))
    vs.log_and_apply(edit)
    vs.close()

    # Reopen and recover.
    vs2 = VersionSet(dbname, opts)
    vs2.recover()
    assert vs2.last_sequence == 55
    assert vs2.log_number == 10
    assert vs2.current.num_files(0) == 1
    assert vs2.current.num_files(1) == 1
    # next_file_number must be past every file we created.
    assert vs2.next_file_number > 4


def test_table_cache_roundtrip(tmp_path):
    dbname = str(tmp_path / "db")
    os.makedirs(dbname)
    opts = Options()
    # Write a real table as file number 5.
    from strata.filenames import table_file_name

    entries = sorted(
        (make_internal_key(f"k{i:03d}".encode(), 1, TYPE_VALUE), f"v{i}".encode())
        for i in range(50)
    )
    entries.sort(key=lambda kv: cmp_to_key(compare_internal_keys)(kv[0]))
    with open(table_file_name(dbname, 5), "wb") as f:
        w = SSTableWriter(f, opts)
        for ik, v in entries:
            w.add(ik, v)
        w.finish()

    cache = TableCache(dbname, opts)
    try:
        assert cache.get(5, b"k010", 10) == (LookupResult.FOUND, b"v10")
        assert cache.get(5, b"missing", 10)[0] == LookupResult.ABSENT
        # Iterator works too.
        it = cache.new_iterator(5)
        it.seek_to_first()
        assert it.valid() and it.key() == b"k000"
    finally:
        cache.close()


def test_version_get_across_levels(tmp_path):
    """A key present in both L0 (new) and L1 (old) returns the L0 value."""
    dbname = str(tmp_path / "db")
    os.makedirs(dbname)
    opts = Options()
    from strata.filenames import table_file_name

    def write_table(number, pairs):
        internal = [(make_internal_key(u, s, TYPE_VALUE), v) for (u, s, v) in pairs]
        internal.sort(key=lambda kv: cmp_to_key(compare_internal_keys)(kv[0]))
        with open(table_file_name(dbname, number), "wb") as f:
            w = SSTableWriter(f, opts)
            for ik, v in internal:
                w.add(ik, v)
            meta = w.finish()
        return FileMetaData(number, meta.file_size, meta.smallest, meta.largest)

    old = write_table(7, [(b"k", 1, b"old")])
    new = write_table(9, [(b"k", 5, b"new")])

    v = Version(opts.num_levels)
    v.files[0] = [new]  # newer level-0 file
    v.files[1] = [old]  # older level-1 file

    cache = TableCache(dbname, opts)
    try:
        assert v.get(b"k", 10, cache) == (LookupResult.FOUND, b"new")
        # A snapshot before the L0 write falls through to the L1 value.
        assert v.get(b"k", 3, cache) == (LookupResult.FOUND, b"old")
    finally:
        cache.close()
