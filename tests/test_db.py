"""End-to-end tests for the database façade."""

from __future__ import annotations

import random

import pytest

import strata
from strata import Options, WriteBatch, WriteOptions
from strata.errors import DBClosed, InvalidArgument, LockError


def test_put_get_delete(db):
    db.put(b"k", b"v")
    assert db.get(b"k") == b"v"
    db.delete(b"k")
    assert db.get(b"k") is None
    assert db.get(b"k", default=b"D") == b"D"


def test_overwrite(db):
    db.put(b"k", b"1")
    db.put(b"k", b"2")
    db.put(b"k", b"3")
    assert db.get(b"k") == b"3"


def test_missing_key_default(db):
    assert db.get(b"absent") is None
    assert db.get(b"absent", default=b"fallback") == b"fallback"


def test_dict_style_api(db):
    db[b"a"] = b"1"
    assert db[b"a"] == b"1"
    assert b"a" in db
    assert b"z" not in db
    del db[b"a"]
    assert b"a" not in db
    with pytest.raises(KeyError):
        _ = db[b"missing"]


def test_write_batch_is_atomic(db):
    batch = WriteBatch()
    batch.put(b"a", b"1")
    batch.put(b"b", b"2")
    batch.delete(b"c")
    db.put(b"c", b"old")
    db.write(batch)
    assert db.get(b"a") == b"1"
    assert db.get(b"b") == b"2"
    assert db.get(b"c") is None


def test_empty_batch_is_noop(db):
    db.write(WriteBatch())  # should not raise or advance state
    assert db.stats()["last_sequence"] == 0


def test_scan_ordered_and_half_open(db):
    for k in [b"d", b"a", b"c", b"b", b"e"]:
        db.put(k, k.upper())
    assert list(db.scan()) == [(b"a", b"A"), (b"b", b"B"), (b"c", b"C"), (b"d", b"D"), (b"e", b"E")]
    assert list(db.scan(start=b"b", end=b"d")) == [(b"b", b"B"), (b"c", b"C")]
    assert list(db.scan(start=b"c")) == [(b"c", b"C"), (b"d", b"D"), (b"e", b"E")]
    assert list(db.scan(end=b"c")) == [(b"a", b"A"), (b"b", b"B")]


def test_scan_skips_deleted(db):
    db.put(b"a", b"1")
    db.put(b"b", b"2")
    db.delete(b"a")
    assert list(db.scan()) == [(b"b", b"2")]


def test_snapshot_isolation(db):
    db.put(b"k", b"before")
    snap = db.snapshot()
    db.put(b"k", b"after")
    assert db.get(b"k") == b"after"
    assert db.get(b"k", options=strata.ReadOptions(snapshot=snap)) == b"before"
    db.release_snapshot(snap)


def test_snapshot_context_manager(db):
    db.put(b"k", b"v1")
    with db.snapshot() as snap:
        db.put(b"k", b"v2")
        assert db.get(b"k", options=strata.ReadOptions(snapshot=snap)) == b"v1"


def test_snapshot_sees_deletes_consistently(db):
    db.put(b"k", b"v")
    snap = db.snapshot()
    db.delete(b"k")
    assert db.get(b"k") is None
    assert db.get(b"k", options=strata.ReadOptions(snapshot=snap)) == b"v"
    db.release_snapshot(snap)


def test_flush_and_compact(db):
    for i in range(500):
        db.put(f"k{i:04d}".encode(), f"v{i}".encode())
    db.flush()
    db.compact()
    assert db.get(b"k0250") == b"v250"
    assert sum(1 for _ in db.scan()) == 500


def test_persistence_across_reopen(dbdir):
    db = strata.open(dbdir)
    for i in range(300):
        db.put(f"k{i:04d}".encode(), f"v{i}".encode())
    db.delete(b"k0100")
    db.close()

    db2 = strata.open(dbdir)
    try:
        assert db2.get(b"k0000") == b"v0"
        assert db2.get(b"k0299") == b"v299"
        assert db2.get(b"k0100") is None
        assert sum(1 for _ in db2.scan()) == 299
    finally:
        db2.close()


def test_persistence_with_on_disk_tables(dbdir):
    opts = Options(write_buffer_size=4096, block_size=512)
    db = strata.open(dbdir, opts)
    for i in range(2000):
        db.put(f"k{i:05d}".encode(), (b"x" * 50))
    db.close()
    assert db  # sanity

    db2 = strata.open(dbdir, opts)
    try:
        assert db2.get(b"k00000") == b"x" * 50
        assert db2.get(b"k01999") == b"x" * 50
        assert sum(1 for _ in db2.scan()) == 2000
    finally:
        db2.close()


def test_sync_write_durability(dbdir):
    db = strata.open(dbdir)
    db.put(b"durable", b"value", options=WriteOptions(sync=True))
    db.close()
    db2 = strata.open(dbdir)
    try:
        assert db2.get(b"durable") == b"value"
    finally:
        db2.close()


def test_lock_prevents_second_open(dbdir):
    db = strata.open(dbdir)
    try:
        with pytest.raises(LockError):
            strata.open(dbdir)
    finally:
        db.close()
    # After close the lock is released and reopening works.
    strata.open(dbdir).close()


def test_create_if_missing_false(dbdir):
    with pytest.raises(InvalidArgument):
        strata.open(dbdir, Options(create_if_missing=False))


def test_error_if_exists(dbdir):
    strata.open(dbdir).close()
    with pytest.raises(InvalidArgument):
        strata.open(dbdir, Options(error_if_exists=True))


def test_operations_after_close_raise(dbdir):
    db = strata.open(dbdir)
    db.close()
    with pytest.raises(DBClosed):
        db.get(b"k")
    with pytest.raises(DBClosed):
        db.put(b"k", b"v")
    # Double close is a no-op.
    db.close()


def test_context_manager(dbdir):
    with strata.open(dbdir) as db:
        db.put(b"k", b"v")
    with strata.open(dbdir) as db:
        assert db.get(b"k") == b"v"


def test_binary_keys_and_values(db):
    key = bytes(range(256))
    value = bytes(reversed(range(256)))
    db.put(key, value)
    assert db.get(key) == value
    db.put(b"", b"empty-key")  # empty key is valid
    assert db.get(b"") == b"empty-key"
    db.put(b"empty-value", b"")
    assert db.get(b"empty-value") == b""


def test_randomised_against_reference(dbdir):
    opts = Options(write_buffer_size=16 * 1024, block_size=1024, max_file_size=32 * 1024)
    db = strata.open(dbdir, opts)
    rng = random.Random(1234)
    reference: dict[bytes, bytes] = {}
    try:
        for _ in range(5000):
            k = f"k{rng.randrange(800):04d}".encode()
            if rng.random() < 0.25:
                db.delete(k)
                reference.pop(k, None)
            else:
                v = str(rng.randrange(10**9)).encode()
                db.put(k, v)
                reference[k] = v
        for k, v in reference.items():
            assert db.get(k) == v
        assert list(db.scan()) == sorted(reference.items())
    finally:
        db.close()


def test_stats_report_levels(db):
    for i in range(100):
        db.put(f"k{i:03d}".encode(), b"v")
    db.flush()
    stats = db.stats()
    assert stats["num_files"] >= 1
    assert stats["last_sequence"] == 100
    assert isinstance(stats["levels"], list)
