"""Versions and the manifest — the metadata that names the live tables.

The memtable and the log hold recent writes; everything older lives in SSTable
files spread across levels. *Which* files make up the database at any instant is
itself state that must survive a crash, so it is journalled.

* :class:`FileMetaData` describes one table file (number, size, key range).
* :class:`Version` is an immutable snapshot of the file layout: a list of files
  per level. Point lookups walk it shallow-to-deep; the first level that holds a
  live/deleted answer for a key wins, because compaction guarantees newer
  versions sit in shallower levels (and newer level-0 files).
* :class:`VersionEdit` is a delta — files added and removed — plus bookkeeping
  counters. Edits are appended to the ``MANIFEST`` log; replaying them rebuilds
  the current :class:`Version`.
* :class:`VersionSet` owns the current version, the counters, and the manifest.

Level-0 files may have overlapping key ranges (they are flushed memtables);
levels ≥ 1 are kept sorted and non-overlapping, so they can be binary-searched.
"""

from __future__ import annotations

from functools import cmp_to_key

from .coding import (
    get_length_prefixed,
    get_varint,
    put_length_prefixed,
    put_varint,
)
from .errors import Corruption
from .filenames import (
    current_file_name,
    descriptor_file_name,
)
from .internalkey import compare_internal_keys, extract_user_key
from .memtable import LookupResult
from .options import Options
from .wal import LogReader, LogWriter

COMPARATOR_NAME = "strata.InternalKeyComparator"

# VersionEdit field tags.
_TAG_COMPARATOR = 1
_TAG_LOG_NUMBER = 2
_TAG_NEXT_FILE_NUMBER = 3
_TAG_LAST_SEQUENCE = 4
_TAG_DELETED_FILE = 5
_TAG_NEW_FILE = 6


class FileMetaData:
    __slots__ = ("number", "file_size", "smallest", "largest")

    def __init__(self, number: int, file_size: int, smallest: bytes, largest: bytes):
        self.number = number
        self.file_size = file_size
        self.smallest = smallest  # smallest internal key
        self.largest = largest  # largest internal key

    def smallest_user_key(self) -> bytes:
        return extract_user_key(self.smallest)

    def largest_user_key(self) -> bytes:
        return extract_user_key(self.largest)

    def __repr__(self) -> str:
        return (
            f"FileMetaData(number={self.number}, size={self.file_size}, "
            f"range={self.smallest_user_key()!r}..{self.largest_user_key()!r})"
        )


class VersionEdit:
    def __init__(self):
        self.comparator_name: str | None = None
        self.log_number: int | None = None
        self.next_file_number: int | None = None
        self.last_sequence: int | None = None
        self.deleted_files: set[tuple[int, int]] = set()  # (level, number)
        self.new_files: list[tuple[int, FileMetaData]] = []  # (level, meta)

    def add_file(self, level: int, meta: FileMetaData) -> None:
        self.new_files.append((level, meta))

    def delete_file(self, level: int, number: int) -> None:
        self.deleted_files.add((level, number))

    def encode(self) -> bytes:
        buf = bytearray()
        if self.comparator_name is not None:
            put_varint(buf, _TAG_COMPARATOR)
            put_length_prefixed(buf, self.comparator_name.encode("utf-8"))
        if self.log_number is not None:
            put_varint(buf, _TAG_LOG_NUMBER)
            put_varint(buf, self.log_number)
        if self.next_file_number is not None:
            put_varint(buf, _TAG_NEXT_FILE_NUMBER)
            put_varint(buf, self.next_file_number)
        if self.last_sequence is not None:
            put_varint(buf, _TAG_LAST_SEQUENCE)
            put_varint(buf, self.last_sequence)
        for level, number in sorted(self.deleted_files):
            put_varint(buf, _TAG_DELETED_FILE)
            put_varint(buf, level)
            put_varint(buf, number)
        for level, meta in self.new_files:
            put_varint(buf, _TAG_NEW_FILE)
            put_varint(buf, level)
            put_varint(buf, meta.number)
            put_varint(buf, meta.file_size)
            put_length_prefixed(buf, meta.smallest)
            put_length_prefixed(buf, meta.largest)
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> VersionEdit:
        edit = cls()
        pos = 0
        n = len(data)
        try:
            while pos < n:
                tag, pos = get_varint(data, pos)
                if tag == _TAG_COMPARATOR:
                    name, pos = get_length_prefixed(data, pos)
                    edit.comparator_name = name.decode("utf-8")
                elif tag == _TAG_LOG_NUMBER:
                    edit.log_number, pos = get_varint(data, pos)
                elif tag == _TAG_NEXT_FILE_NUMBER:
                    edit.next_file_number, pos = get_varint(data, pos)
                elif tag == _TAG_LAST_SEQUENCE:
                    edit.last_sequence, pos = get_varint(data, pos)
                elif tag == _TAG_DELETED_FILE:
                    level, pos = get_varint(data, pos)
                    number, pos = get_varint(data, pos)
                    edit.deleted_files.add((level, number))
                elif tag == _TAG_NEW_FILE:
                    level, pos = get_varint(data, pos)
                    number, pos = get_varint(data, pos)
                    file_size, pos = get_varint(data, pos)
                    smallest, pos = get_length_prefixed(data, pos)
                    largest, pos = get_length_prefixed(data, pos)
                    edit.add_file(level, FileMetaData(number, file_size, smallest, largest))
                else:
                    raise Corruption(f"unknown VersionEdit tag {tag}")
        except ValueError as exc:  # truncated varint / slice
            raise Corruption(f"corrupt VersionEdit: {exc}") from exc
        return edit


class Version:
    """An immutable snapshot of which table files live at each level."""

    def __init__(self, num_levels: int):
        self.files: list[list[FileMetaData]] = [[] for _ in range(num_levels)]

    @property
    def num_levels(self) -> int:
        return len(self.files)

    def num_files(self, level: int) -> int:
        return len(self.files[level])

    def total_file_size(self, level: int) -> int:
        return sum(f.file_size for f in self.files[level])

    def get(self, user_key: bytes, sequence: int, table_cache) -> tuple[str, bytes | None]:
        """Point lookup walking levels shallow-to-deep."""
        # Level 0: files overlap; search newest (highest number) first.
        level0 = [
            f
            for f in self.files[0]
            if f.smallest_user_key() <= user_key <= f.largest_user_key()
        ]
        level0.sort(key=lambda f: f.number, reverse=True)
        for f in level0:
            status, value = table_cache.get(f.number, user_key, sequence)
            if status != LookupResult.ABSENT:
                return status, value

        # Levels >= 1: non-overlapping and sorted; binary search one candidate.
        for level in range(1, self.num_levels):
            files = self.files[level]
            if not files:
                continue
            idx = _find_file(files, user_key)
            if idx < len(files) and files[idx].smallest_user_key() <= user_key:
                status, value = table_cache.get(files[idx].number, user_key, sequence)
                if status != LookupResult.ABSENT:
                    return status, value
        return LookupResult.ABSENT, None

    def overlapping_inputs(
        self, level: int, begin_user: bytes | None, end_user: bytes | None
    ) -> list[FileMetaData]:
        """Files at ``level`` whose key range intersects ``[begin, end]``."""
        out = []
        for f in self.files[level]:
            if begin_user is not None and f.largest_user_key() < begin_user:
                continue
            if end_user is not None and f.smallest_user_key() > end_user:
                continue
            out.append(f)
        return out

    def pick_compaction_level(self, options: Options):
        """Return ``(level, score)`` most in need of compaction, or ``None``."""
        best_level, best_score = -1, -1.0
        for level in range(self.num_levels - 1):
            if level == 0:
                score = self.num_files(0) / options.level0_file_num_compaction_trigger
            else:
                score = self.total_file_size(level) / max_bytes_for_level(options, level)
            if score > best_score:
                best_level, best_score = level, score
        if best_score >= 1.0:
            return best_level, best_score
        return None

    def level_summary(self) -> list[dict]:
        return [
            {"level": i, "files": len(level), "bytes": sum(f.file_size for f in level)}
            for i, level in enumerate(self.files)
        ]


def _find_file(files: list[FileMetaData], user_key: bytes) -> int:
    """Index of the first file whose largest user key is >= ``user_key``."""
    lo, hi = 0, len(files)
    while lo < hi:
        mid = (lo + hi) // 2
        if files[mid].largest_user_key() < user_key:
            lo = mid + 1
        else:
            hi = mid
    return lo


def max_bytes_for_level(options: Options, level: int) -> int:
    """Byte budget for ``level`` (>= 1). Level 1 is the base; each is ×mult."""
    result = options.max_bytes_for_level_base
    for _ in range(level - 1):
        result *= options.max_bytes_for_level_multiplier
    return result


class VersionSet:
    """Owns the current version, the shared counters, and the manifest log."""

    def __init__(self, dbname: str, options: Options):
        self.dbname = dbname
        self.options = options
        self.comparator_name = COMPARATOR_NAME
        self.next_file_number = 2  # 1 is reserved for the first manifest
        self.manifest_file_number = 0
        self.last_sequence = 0
        self.log_number = 0
        self._current = Version(options.num_levels)
        self._manifest_file = None
        self._manifest_writer: LogWriter | None = None

    @property
    def current(self) -> Version:
        return self._current

    def new_file_number(self) -> int:
        number = self.next_file_number
        self.next_file_number += 1
        return number

    def mark_file_number_used(self, number: int) -> None:
        if self.next_file_number <= number:
            self.next_file_number = number + 1

    def _apply_edit(self, base: Version, edit: VersionEdit) -> Version:
        new_version = Version(self.options.num_levels)
        for level in range(self.options.num_levels):
            new_version.files[level] = list(base.files[level])
        for level, number in edit.deleted_files:
            new_version.files[level] = [
                f for f in new_version.files[level] if f.number != number
            ]
        for level, meta in edit.new_files:
            new_version.files[level].append(meta)
        # Keep levels >= 1 sorted by smallest internal key (non-overlapping).
        ikey_cmp = cmp_to_key(compare_internal_keys)
        for level in range(1, self.options.num_levels):
            new_version.files[level].sort(key=lambda f: ikey_cmp(f.smallest))
        return new_version

    def log_and_apply(self, edit: VersionEdit, sync: bool = True) -> None:
        """Persist ``edit`` to the manifest and swap in the new version."""
        if edit.log_number is None:
            edit.log_number = self.log_number
        edit.next_file_number = self.next_file_number
        edit.last_sequence = self.last_sequence
        edit.comparator_name = self.comparator_name

        new_version = self._apply_edit(self._current, edit)
        assert self._manifest_writer is not None, "manifest not open"
        self._manifest_writer.add_record(edit.encode(), sync=sync)
        self._current = new_version
        self.log_number = edit.log_number

    def create_new_manifest(self) -> None:
        """Start a fresh manifest capturing the whole current state."""
        if self._manifest_writer is not None:
            self._manifest_writer.close()
            self._manifest_writer = None
        self.manifest_file_number = self.new_file_number()
        path = descriptor_file_name(self.dbname, self.manifest_file_number)
        self._manifest_file = open(path, "wb")
        self._manifest_writer = LogWriter(self._manifest_file)
        self._write_snapshot()
        self._set_current_file()

    def _write_snapshot(self) -> None:
        snapshot = VersionEdit()
        snapshot.comparator_name = self.comparator_name
        snapshot.log_number = self.log_number
        snapshot.next_file_number = self.next_file_number
        snapshot.last_sequence = self.last_sequence
        for level in range(self.options.num_levels):
            for meta in self._current.files[level]:
                snapshot.add_file(level, meta)
        self._manifest_writer.add_record(snapshot.encode(), sync=True)

    def _set_current_file(self) -> None:
        name = f"MANIFEST-{self.manifest_file_number:06d}\n"
        tmp = current_file_name(self.dbname) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(name)
            f.flush()
        import os

        os.replace(tmp, current_file_name(self.dbname))

    def recover(self) -> None:
        """Rebuild the current version by replaying the manifest."""
        with open(current_file_name(self.dbname), encoding="utf-8") as f:
            manifest_name = f.read().strip()
        manifest_path = descriptor_file_name(
            self.dbname, int(manifest_name.split("-")[1])
        )
        version = Version(self.options.num_levels)
        log_number = last_sequence = next_file = None
        comparator = None
        with open(manifest_path, "rb") as f:
            for record in LogReader(f, paranoid=self.options.paranoid_checks):
                edit = VersionEdit.decode(record)
                version = self._apply_edit(version, edit)
                if edit.comparator_name is not None:
                    comparator = edit.comparator_name
                if edit.log_number is not None:
                    log_number = edit.log_number
                if edit.last_sequence is not None:
                    last_sequence = edit.last_sequence
                if edit.next_file_number is not None:
                    next_file = edit.next_file_number

        if comparator is not None and comparator != self.comparator_name:
            raise Corruption(
                f"comparator mismatch: database uses {comparator!r}, "
                f"engine uses {self.comparator_name!r}"
            )
        self._current = version
        if log_number is not None:
            self.log_number = log_number
        if last_sequence is not None:
            self.last_sequence = last_sequence
        if next_file is not None:
            self.mark_file_number_used(next_file - 1)
        # Ensure every known file number is accounted for.
        for level in range(self.options.num_levels):
            for meta in version.files[level]:
                self.mark_file_number_used(meta.number)
        self.mark_file_number_used(int(manifest_name.split("-")[1]))

    def close(self) -> None:
        if self._manifest_writer is not None:
            self._manifest_writer.close()
            self._manifest_writer = None


__all__ = [
    "FileMetaData",
    "VersionEdit",
    "Version",
    "VersionSet",
    "max_bytes_for_level",
    "COMPARATOR_NAME",
]
