# Roadmap

Strata has a correct, tested core. There is a lot of worthwhile engine work
ahead — this is a living document, roughly in priority order.

## Done (0.1)

- [x] Write-ahead log with per-record CRC and torn-tail recovery
- [x] Skip-list memtable with MVCC sequence numbers
- [x] SSTables: prefix-compressed blocks, restart points, per-table Bloom
      filter, per-block CRCs, footer
- [x] Manifest + version set journalling the live file set
- [x] Leveled compaction with snapshot-aware garbage collection
- [x] Atomic write batches
- [x] Consistent MVCC snapshots and ordered range scans
- [x] Crash recovery, directory locking, obsolete-file cleanup
- [x] CLI: `get` / `put` / `delete` / `scan` / `stats` / `compact` / `inspect` / `bench`

## Next

- [ ] **Background compaction** — move flush and compaction off the write path
      onto a worker thread, with write throttling/stalls at the level-0 triggers
      (the hooks and counters are already in `Options`).
- [ ] **Reverse iteration** — `scan(..., reverse=True)`; needs a bidirectional
      block iterator and a reverse-capable merging iterator.
- [ ] **Block cache** — a shared, size-bounded LRU of decoded blocks across
      tables (today each reader caches its own blocks).
- [ ] **Compaction pointers** — spread level-N compaction across the key space
      instead of always taking the first file, and expand level-0 inputs to the
      full overlap, to even out write amplification.
- [ ] **Flush to the deepest non-overlapping level** (`PickLevelForMemTableOutput`)
      so fresh tables skip levels they don't overlap.

## Later

- [ ] **Compression** — pluggable per-block compression (zlib in stdlib; zstd
      optional) behind the compression-type byte the block trailer already
      carries.
- [ ] **Block-fragmented log** — switch the WAL to LevelDB's 32 KiB block
      framing so a corrupt record can be skipped rather than ending recovery.
- [ ] **Manifest compaction** — periodically rewrite the manifest so it does not
      grow unbounded across many edits.
- [ ] **Custom comparators** — allow a user-supplied key ordering (stored in the
      manifest and checked on open, as the comparator name already is).
- [ ] **Column families** — independent keyspaces sharing one WAL and manifest.
- [ ] **Statistics & tracing** — read/write amplification, per-level compaction
      counters, a `GetProperty`-style introspection API.
- [ ] **Checksummed snapshots / backup** — consistent on-disk backup via hard
      links to immutable tables plus a manifest snapshot.
- [ ] **Packaging** — publish to PyPI with wheels; a typed public API stub.

## Non-goals

- Not a client/server database — Strata is an embedded, single-process library.
- No attempt to match native-code engines on raw throughput; clarity and
  correctness come first.
- No networking, no telemetry, no required configuration.
