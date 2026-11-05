"""Tunable options controlling engine behaviour.

The defaults are chosen to be reasonable for a few-hundred-megabyte embedded
store on commodity hardware. They mirror the knobs exposed by LevelDB/RocksDB
so the trade-offs are familiar, while staying small enough to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Options:
    """Database-wide options, fixed for the lifetime of an open handle."""

    # Create the database directory if it does not already exist.
    create_if_missing: bool = True
    # Fail to open if the database directory already exists.
    error_if_exists: bool = False
    # Verify checksums aggressively and refuse to silently drop corrupt data.
    paranoid_checks: bool = False

    # Size (bytes) a memtable may reach before it is frozen and flushed to an
    # on-disk table. Larger values amortise writes but use more memory and
    # lengthen recovery.
    write_buffer_size: int = 4 * 1024 * 1024

    # Approximate uncompressed size (bytes) of a data block within a table.
    block_size: int = 4 * 1024
    # Full keys are stored every ``block_restart_interval`` entries so a block
    # can be binary-searched; entries in between are prefix-compressed.
    block_restart_interval: int = 16

    # Target size (bytes) for an on-disk table file. Compaction output is split
    # into files no larger than this.
    max_file_size: int = 2 * 1024 * 1024

    # Bits of Bloom filter per key (0 disables the filter). 10 bits gives a
    # ~1% false-positive rate.
    bloom_bits_per_key: int = 10

    # Maximum number of table files kept open by the table cache.
    max_open_files: int = 500

    # Number of levels in the LSM tree.
    num_levels: int = 7
    # Level-0 is compacted once it holds this many files.
    level0_file_num_compaction_trigger: int = 4
    # Level-0 slows writers / is urgent beyond these counts (advisory in 0.1).
    level0_slowdown_writes_trigger: int = 8
    level0_stop_writes_trigger: int = 12
    # Byte budget for level-1; each deeper level is this multiplier larger.
    max_bytes_for_level_base: int = 10 * 1024 * 1024
    max_bytes_for_level_multiplier: int = 10

    def __post_init__(self) -> None:
        if self.write_buffer_size <= 0:
            raise ValueError("write_buffer_size must be positive")
        if self.block_size <= 0:
            raise ValueError("block_size must be positive")
        if self.block_restart_interval <= 0:
            raise ValueError("block_restart_interval must be positive")
        if self.num_levels < 2:
            raise ValueError("num_levels must be at least 2")
        if self.bloom_bits_per_key < 0:
            raise ValueError("bloom_bits_per_key must be non-negative")


@dataclass
class ReadOptions:
    """Per-read options."""

    # Verify the CRC of every block touched by the read.
    verify_checksums: bool = False
    # Populate the block/table caches with data touched by the read.
    fill_cache: bool = True
    # Read as of this snapshot (see :meth:`strata.db.DB.snapshot`). ``None``
    # reads the latest committed state.
    snapshot: object | None = None


@dataclass
class WriteOptions:
    """Per-write options."""

    # fsync the write-ahead log before the write is acknowledged. Durable
    # across power loss, but much slower. Off by default (durable across a
    # process crash, not a kernel/power crash).
    sync: bool = False


# A shared default so callers that don't care never allocate.
DEFAULT_READ_OPTIONS = ReadOptions()
DEFAULT_WRITE_OPTIONS = WriteOptions()


__all__ = [
    "Options",
    "ReadOptions",
    "WriteOptions",
    "DEFAULT_READ_OPTIONS",
    "DEFAULT_WRITE_OPTIONS",
]
