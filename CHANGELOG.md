# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-05

Initial release: a functional, tested LSM storage engine.

### Added

- Ordered, durable `bytes → bytes` store with `get`, `put`, `delete`, atomic
  `WriteBatch`, MVCC `snapshot`, and ordered range `scan`.
- Write-ahead log with per-record masked CRC-32 and clean torn-tail recovery;
  optional `sync=True` fsync for power-loss durability.
- Skip-list memtable keyed by multi-version internal keys.
- SSTables with prefix-compressed, restart-indexed data blocks, a per-table
  Bloom filter, per-block CRCs, and a fixed footer.
- Manifest + version set journalling the live file set, with crash recovery.
- Leveled compaction (minor flush and major merge) with snapshot-aware garbage
  collection of superseded versions and obsolete deletion tombstones.
- LRU table cache, directory locking (advisory `flock` on POSIX), and
  obsolete-file cleanup.
- Command-line interface: `get`, `put`, `delete`, `scan`, `stats`, `compact`,
  `inspect`, `bench`, with `--json` and `--hex` modes.
- Zero runtime dependencies (Python standard library only); CI on Python
  3.10–3.13.

[Unreleased]: https://github.com/Martin00309/strata/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Martin00309/strata/releases/tag/v0.1.0
