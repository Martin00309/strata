"""Compaction — turning the write buffer and overlapping tables into levels.

Two things reshape the on-disk tree:

* a **minor compaction** (``flush_memtable``) writes a frozen memtable out as a
  new level-0 table; and
* a **major compaction** (``pick_compaction`` + ``run_compaction``) merges a
  level's files with the overlapping files one level down, discarding versions
  no snapshot can see and dropping deletion tombstones once they reach the
  bottom of the tree.

The garbage-collection rule during a major compaction is the classic LSM one:
walking the merged, newest-first stream, for each user key keep the first
version at or below the oldest live snapshot and drop everything older; drop a
deletion marker outright once it is older than every snapshot *and* no deeper
level still holds the key (otherwise the delete would resurrect stale data).
"""

from __future__ import annotations

import os

from .filenames import table_file_name
from .internalkey import (
    MAX_SEQUENCE,
    TYPE_DELETION,
    parse_internal_key,
)
from .iterator import MergingIterator
from .options import Options
from .sstable import SSTableWriter
from .version import FileMetaData, Version, VersionEdit


def write_table(dbname: str, options: Options, number: int, entries) -> FileMetaData:
    """Write ``entries`` (``(internal_key, value)`` in order) as table ``number``."""
    path = table_file_name(dbname, number)
    with open(path, "wb") as f:
        writer = SSTableWriter(f, options)
        for internal_key, value in entries:
            writer.add(internal_key, value)
        meta = writer.finish()
        f.flush()
        os.fsync(f.fileno())
    return FileMetaData(number, meta.file_size, meta.smallest, meta.largest)


def flush_memtable(dbname: str, options: Options, number: int, memtable) -> FileMetaData:
    """Minor compaction: persist a frozen memtable as a new table file."""
    return write_table(dbname, options, number, memtable.entries())


class Compaction:
    """A planned major compaction: files at ``level`` + overlap at ``level+1``."""

    def __init__(self, level: int, inputs: list[list[FileMetaData]]):
        self.level = level
        self.inputs = inputs  # inputs[0]: level files, inputs[1]: level+1 files

    @property
    def output_level(self) -> int:
        return self.level + 1

    def is_trivial_move(self) -> bool:
        # A single source file with no overlap below can just be relabelled.
        return len(self.inputs[0]) == 1 and len(self.inputs[1]) == 0

    def all_inputs(self) -> list[FileMetaData]:
        return self.inputs[0] + self.inputs[1]


def pick_compaction(version: Version, options: Options) -> Compaction | None:
    """Choose the next major compaction, or ``None`` if the tree is balanced."""
    picked = version.pick_compaction_level(options)
    if picked is None:
        return None
    level, _score = picked

    if level == 0:
        # Level-0 files overlap each other, so compact all of them together.
        inputs0 = list(version.files[0])
    else:
        # Pick the first file at this level (a compaction pointer that spreads
        # the work across the key space is on the roadmap).
        inputs0 = [version.files[level][0]]

    if not inputs0:
        return None

    begin, end = _range_of(inputs0)
    inputs1 = version.overlapping_inputs(level + 1, begin, end)
    return Compaction(level, [inputs0, inputs1])


def run_compaction(
    dbname: str,
    options: Options,
    version: Version,
    table_cache,
    compaction: Compaction,
    smallest_snapshot: int,
    next_file_number,
) -> VersionEdit:
    """Execute ``compaction`` and return the :class:`VersionEdit` describing it.

    ``next_file_number`` is a zero-arg callable that allocates a fresh file
    number for each output table.
    """
    edit = VersionEdit()
    for f in compaction.inputs[0]:
        edit.delete_file(compaction.level, f.number)
    for f in compaction.inputs[1]:
        edit.delete_file(compaction.output_level, f.number)

    if compaction.is_trivial_move():
        moved = compaction.inputs[0][0]
        edit.add_file(compaction.output_level, moved)
        return edit

    merged = MergingIterator(
        [table_cache.new_iterator(f.number) for f in compaction.all_inputs()]
    )
    merged.seek_to_first()

    # Monotonic per-level cursors used by the "is this the base level for the
    # key?" check; valid because we emit keys in increasing user-key order.
    deeper_levels = range(compaction.output_level + 1, version.num_levels)
    level_ptrs = dict.fromkeys(deeper_levels, 0)

    def is_base_level_for_key(user_key: bytes) -> bool:
        for lvl in deeper_levels:
            files = version.files[lvl]
            ptr = level_ptrs[lvl]
            while ptr < len(files) and files[ptr].largest_user_key() < user_key:
                ptr += 1
            level_ptrs[lvl] = ptr
            if ptr < len(files) and files[ptr].smallest_user_key() <= user_key:
                return False
        return True

    outputs: list[FileMetaData] = []
    writer = None
    output_file = None

    def open_output():
        nonlocal writer, output_file
        number = next_file_number()
        path = table_file_name(dbname, number)
        output_file = open(path, "wb")
        writer = SSTableWriter(output_file, options)
        return number

    def finish_output(number):
        nonlocal writer, output_file
        meta = writer.finish()
        output_file.flush()
        os.fsync(output_file.fileno())
        output_file.close()
        outputs.append(FileMetaData(number, meta.file_size, meta.smallest, meta.largest))
        writer = None
        output_file = None

    current_number = None
    current_user_key = None
    last_sequence_for_key = MAX_SEQUENCE

    while merged.valid():
        internal_key = merged.internal_key()
        user_key, seq, vtype = parse_internal_key(internal_key)

        if current_user_key is None or user_key != current_user_key:
            current_user_key = user_key
            last_sequence_for_key = MAX_SEQUENCE

        drop = False
        if last_sequence_for_key <= smallest_snapshot:
            # A newer version of this key (already emitted) hides this one.
            drop = True
        elif (
            vtype == TYPE_DELETION
            and seq <= smallest_snapshot
            and is_base_level_for_key(user_key)
        ):
            # Obsolete tombstone with nothing older beneath it.
            drop = True

        last_sequence_for_key = seq

        if not drop:
            if writer is None:
                current_number = open_output()
            writer.add(internal_key, merged.value())
            if writer.current_size >= options.max_file_size:
                finish_output(current_number)

        merged.next()

    if writer is not None:
        finish_output(current_number)

    for meta in outputs:
        edit.add_file(compaction.output_level, meta)
    return edit


def _range_of(files: list[FileMetaData]) -> tuple[bytes, bytes]:
    begin = min(f.smallest_user_key() for f in files)
    end = max(f.largest_user_key() for f in files)
    return begin, end


__all__ = [
    "Compaction",
    "pick_compaction",
    "run_compaction",
    "flush_memtable",
    "write_table",
]
