# Strata

[![CI](https://github.com/Martin00309/strata/actions/workflows/ci.yml/badge.svg)](https://github.com/Martin00309/strata/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

**Strata** is an embedded, ordered, crash-safe key-value storage engine built
on a log-structured merge-tree (LSM). It gives you a durable, transactional
`bytes → bytes` map with consistent snapshots and range scans — the kind of
engine that sits underneath a database — in **pure Python with zero runtime
dependencies**.

> Status: early (**0.1, alpha**). The engine is functional and tested
> (150+ tests), and validated against a reference model under randomized
> workloads. The [roadmap](ROADMAP.md) tracks the work ahead.

## Why Strata

Most Python key-value options are either a thin wrapper around a C library
(LevelDB/RocksDB bindings, LMDB) or a simple hash-on-disk (`dbm`, `shelve`)
that gives you none of ordering, snapshots, or crash safety. Strata is a
from-scratch, readable implementation of the real thing — a leveled LSM engine
— so you can:

- embed an ordered, durable store without native build dependencies;
- read a complete, documented LSM engine end to end (it's meant to be studied
  as much as used); and
- extend it — the layers are small and decoupled.

It is **not** trying to beat RocksDB on throughput. It is trying to be correct,
clear, and dependency-free.

## Features

- **Ordered** `bytes → bytes` store with `get`, `put`, `delete`, and ordered
  **range scans** (`[start, end)`).
- **Durability** via a write-ahead log with per-record CRCs; recovery replays
  the log and drops a torn (crash-truncated) tail cleanly. `sync=True` fsyncs
  for power-loss durability.
- **Atomic write batches** — many puts/deletes committed as one unit.
- **MVCC snapshots** — take a snapshot and read a consistent point-in-time view
  while writes continue.
- **Leveled compaction** with snapshot-aware garbage collection: obsolete
  versions and tombstones are reclaimed once no snapshot can see them.
- **SSTables** with prefix-compressed, restart-indexed blocks, a per-table
  **Bloom filter** to skip absent-key reads, and per-block CRCs.
- **A manifest** that journals the set of live files, so the on-disk layout
  survives crashes.
- A scriptable **CLI** (`strata get/put/scan/stats/compact/inspect/bench`).
- **Zero runtime dependencies** — the Python standard library only.

## Installation

Requires Python 3.10+.

```bash
git clone https://github.com/Martin00309/strata.git
cd strata
pip install -e .
```

Or run straight from a checkout without installing:

```bash
PYTHONPATH=src python -m strata --help
```

## Library usage

```python
import strata

with strata.open("./mydb") as db:
    # Writes
    db.put(b"user:1", b'{"name": "Ada"}')
    db[b"user:2"] = b'{"name": "Linus"}'  # dict-style sugar

    # Reads
    print(db.get(b"user:1"))  # b'{"name": "Ada"}'
    print(db.get(b"missing", default=b"{}"))  # b'{}'
    print(b"user:2" in db)  # True

    # Atomic batch
    batch = strata.WriteBatch()
    batch.put(b"a", b"1").put(b"b", b"2").delete(b"user:2")
    db.write(batch)

    # Ordered range scan (end is exclusive)
    for key, value in db.scan(start=b"user:", end=b"user;"):
        print(key, value)

    # Consistent snapshot
    snap = db.snapshot()
    db.put(b"user:1", b"changed")
    old = db.get(b"user:1", options=strata.ReadOptions(snapshot=snap))
    assert old == b'{"name": "Ada"}'
    db.release_snapshot(snap)

    # Durable write (fsync the log before returning)
    db.put(b"important", b"value", options=strata.WriteOptions(sync=True))
```

Keys and values are arbitrary `bytes` (including empty); keys are ordered by
plain lexicographic byte comparison.

## Command-line usage

```bash
strata put ./db user:1 '{"name": "Ada"}'
strata get ./db user:1
strata scan ./db --start user: --limit 20
strata delete ./db user:1
strata stats ./db                 # sequence, memtable, per-level file counts/sizes
strata compact ./db               # flush the memtable and fully compact
strata inspect ./db               # list the files that make up the store
strata bench ./db --count 100000  # quick write/read micro-benchmark

# Binary-safe keys/values via hex, machine-readable output via --json
strata put ./db deadbeef 00ff10 --hex
strata get ./db user:1 --json
```

Exit codes: `0` success, `2` invalid input/usage, `3` key not found.

## How it works

A write is appended to the **write-ahead log** and inserted into an in-memory
**memtable** (a skip list). When the memtable fills, it is frozen and flushed
to a sorted **SSTable** at level 0. **Compaction** later merges overlapping
tables down through the levels, discarding versions and deletion tombstones
that no live snapshot needs. A read checks the memtable, then level 0
(newest first), then each deeper level — consulting each table's Bloom filter
and block index so most reads touch at most one block of one file.

```
          put/delete
              │
              ▼
      ┌───────────────┐   fsync-optional
      │ write-ahead log│──────────────────►  durability
      └───────────────┘
              │
              ▼
      ┌───────────────┐   fills up, frozen & flushed
      │   memtable     │──────────────────►  level 0 SSTable
      │  (skip list)   │
      └───────────────┘
                                   compaction (merge + GC)
   level 0  ┌──┐┌──┐┌──┐  ───────────────────────────────►  level 1 ┌────┐
   level 1  ┌────┐┌────┐  ───────────────────────────────►  level 2 ┌──────┐
     ...        (non-overlapping, ×10 bytes per level)
```

The components map directly onto modules:

| Concern | Module |
| --- | --- |
| Encoding primitives (varint, CRC, …) | `strata.coding` |
| Multi-version keys & ordering | `strata.internalkey` |
| In-memory write buffer | `strata.skiplist`, `strata.memtable` |
| Durability | `strata.wal`, `strata.batch` |
| On-disk tables | `strata.block`, `strata.bloom`, `strata.sstable` |
| Metadata journal | `strata.version` (manifest + versions), `strata.filenames` |
| Reshaping the tree | `strata.compaction`, `strata.table_cache` |
| Merge & snapshot reads | `strata.iterator` |
| Public engine | `strata.db` |

The on-disk format is specified in [docs/format.md](docs/format.md).

## Development

```bash
pip install -e ".[dev]"
pytest -ra            # run the test suite
ruff check .          # lint
ruff format .         # format
```

The test suite uses only `pytest`; the engine itself imports nothing outside
the standard library.

## Roadmap & contributing

Planned work is in [ROADMAP.md](ROADMAP.md); contribution guidelines are in
[CONTRIBUTING.md](CONTRIBUTING.md). Issues and pull requests are welcome.

## License

Released under the [MIT License](LICENSE).
