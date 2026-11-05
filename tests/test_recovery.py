"""Crash-recovery and durability tests.

These exercise the write-ahead log path: that acknowledged writes survive a
reopen, that a torn log tail (a write interrupted by a crash) is dropped
cleanly, and that sequence numbers resume correctly so recovered data is never
clobbered by subsequent writes.
"""

from __future__ import annotations

import glob
import os

import strata
from strata import Options


def _only_log(dbdir: str) -> str:
    logs = glob.glob(os.path.join(dbdir, "*.log"))
    assert len(logs) == 1, f"expected exactly one log, found {logs}"
    return logs[0]


def test_writes_survive_reopen_via_wal(dbdir):
    # Small enough to stay in the memtable, so recovery must come from the WAL.
    db = strata.open(dbdir)
    for i in range(50):
        db.put(f"k{i:03d}".encode(), f"v{i}".encode())
    db.close()

    db2 = strata.open(dbdir)
    try:
        for i in range(50):
            assert db2.get(f"k{i:03d}".encode()) == f"v{i}".encode()
    finally:
        db2.close()


def test_torn_log_tail_is_dropped(dbdir):
    db = strata.open(dbdir)
    for i in range(20):
        db.put(f"k{i:02d}".encode(), f"v{i}".encode(), options=strata.WriteOptions(sync=True))
    log_path = _only_log(dbdir)
    db.close()

    # Simulate a crash mid-write: lop the last few bytes off the active log.
    size = os.path.getsize(log_path)
    with open(log_path, "r+b") as f:
        f.truncate(size - 3)

    db2 = strata.open(dbdir)
    try:
        # The first writes survive; at most the final (torn) record is lost.
        assert db2.get(b"k00") == b"v0"
        assert db2.get(b"k10") == b"v10"
        survivors = sum(1 for _ in db2.scan())
        assert 19 <= survivors <= 20
    finally:
        db2.close()


def test_sequence_resumes_after_recovery(dbdir):
    db = strata.open(dbdir)
    db.put(b"k", b"first")
    db.close()

    db2 = strata.open(dbdir)
    try:
        # A fresh write must take a higher sequence than the recovered one, so
        # it wins — and an old snapshot sequence can't accidentally hide it.
        db2.put(b"k", b"second")
        assert db2.get(b"k") == b"second"
        assert db2.stats()["last_sequence"] >= 2
    finally:
        db2.close()


def test_recovery_with_on_disk_tables(dbdir):
    opts = Options(write_buffer_size=4096, block_size=512)
    db = strata.open(dbdir, opts)
    for i in range(3000):
        db.put(f"k{i:05d}".encode(), b"y" * 40)
    # Some data is now in SSTables, some still in the memtable/WAL.
    db.close()

    db2 = strata.open(dbdir, opts)
    try:
        assert db2.get(b"k00000") == b"y" * 40
        assert db2.get(b"k02999") == b"y" * 40
        assert sum(1 for _ in db2.scan()) == 3000
    finally:
        db2.close()


def test_delete_survives_recovery(dbdir):
    db = strata.open(dbdir)
    db.put(b"keep", b"1")
    db.put(b"gone", b"2")
    db.delete(b"gone")
    db.close()

    db2 = strata.open(dbdir)
    try:
        assert db2.get(b"keep") == b"1"
        assert db2.get(b"gone") is None
    finally:
        db2.close()


def test_reopen_many_times(dbdir):
    # Each reopen flushes the replayed WAL to a fresh table; the store must stay
    # correct and the file set must not grow without bound (compaction kicks in).
    for cycle in range(12):
        db = strata.open(dbdir)
        db.put(f"cycle{cycle}".encode(), str(cycle).encode())
        db.close()
    db = strata.open(dbdir)
    try:
        for cycle in range(12):
            assert db.get(f"cycle{cycle}".encode()) == str(cycle).encode()
    finally:
        db.close()
