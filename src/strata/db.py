"""The database — the public façade that ties the engine together.

:class:`DB` owns the write path (WAL → memtable → flush → compaction), the read
path (memtable → levels), snapshots, range scans, recovery and the on-disk
lifecycle. A single reentrant lock serialises mutation and the structural parts
of reads; flushes and compactions run synchronously under that lock in 0.1
(moving them to a background thread is on the roadmap).

Durability model: a ``put``/``delete`` is acknowledged once its batch is in the
write-ahead log. With ``WriteOptions(sync=True)`` the log is fsynced first, so
the write survives power loss; by default it survives a process crash (the OS
still holds the buffered bytes) but a simultaneous power loss could lose the
last unsynced writes. Recovery replays the log, so no acknowledged *synced*
write is ever lost.
"""

from __future__ import annotations

import os
import threading

try:
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX platforms
    _HAVE_FCNTL = False

from .batch import WriteBatch, insert_into_memtable
from .compaction import flush_memtable, pick_compaction, run_compaction
from .errors import DBClosed, InvalidArgument, LockError
from .filenames import (
    current_file_name,
    lock_file_name,
    log_file_name,
    parse_file_name,
    table_file_name,
)
from .iterator import DBIterator, MergingIterator
from .memtable import LookupResult, MemTable
from .options import DEFAULT_READ_OPTIONS, DEFAULT_WRITE_OPTIONS, Options, ReadOptions, WriteOptions
from .sstable import SSTableReader
from .table_cache import TableCache
from .version import VersionEdit, VersionSet
from .wal import LogReader, LogWriter

# Safety cap on a single compaction-scheduling drain, to guarantee termination
# even if options are configured pathologically.
_MAX_COMPACTIONS_PER_DRAIN = 10_000


class Snapshot:
    """A handle to a point-in-time view of the database (a sequence number)."""

    __slots__ = ("sequence", "_db")

    def __init__(self, sequence: int, db: DB):
        self.sequence = sequence
        self._db = db

    def __enter__(self) -> Snapshot:
        return self

    def __exit__(self, *exc) -> None:
        self._db.release_snapshot(self)


class DB:
    def __init__(self, name: str, options: Options):
        self._name = name
        self._options = options
        self._lock = threading.RLock()
        self._closed = False
        self._mem = MemTable()
        self._vset = VersionSet(name, options)
        self._table_cache = TableCache(name, options)
        self._log_file = None
        self._log_writer: LogWriter | None = None
        self._lock_fd: int | None = None
        self._snapshots: set[Snapshot] = set()

    # ------------------------------------------------------------------ open #
    @classmethod
    def open(cls, name: str, options: Options | None = None) -> DB:
        options = options or Options()
        current_exists = os.path.exists(current_file_name(name))

        if not os.path.exists(name):
            if not options.create_if_missing:
                raise InvalidArgument(f"database {name!r} does not exist")
            os.makedirs(name, exist_ok=True)
        if current_exists and options.error_if_exists:
            raise InvalidArgument(f"database {name!r} already exists")
        if not current_exists and not options.create_if_missing:
            raise InvalidArgument(f"database {name!r} does not exist")

        db = cls(name, options)
        db._acquire_lock()
        try:
            if current_exists:
                db._recover()
            else:
                db._initialise()
            db._delete_obsolete_files()
        except Exception:
            db._release_lock()
            raise
        return db

    def _acquire_lock(self) -> None:
        path = lock_file_name(self._name)
        locked_msg = (
            f"database {self._name!r} is locked by another handle "
            f"(remove {path} if no process is using it)"
        )
        if _HAVE_FCNTL:
            # An advisory lock releases automatically if the process dies, so a
            # crash never leaves a stale lock behind.
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                raise LockError(locked_msg) from exc
        else:  # pragma: no cover - non-POSIX fallback
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
            except FileExistsError as exc:
                raise LockError(locked_msg) from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
                os.remove(lock_file_name(self._name))
            except OSError:
                pass
            self._lock_fd = None

    def _initialise(self) -> None:
        """Create the files for a brand-new database."""
        log_number = self._vset.new_file_number()
        self._vset.log_number = log_number
        self._vset.create_new_manifest()
        self._open_log(log_number)

    def _recover(self) -> None:
        self._vset.recover()
        max_sequence = self._vset.last_sequence

        # Replay the active write-ahead log into a fresh memtable.
        log_path = log_file_name(self._name, self._vset.log_number)
        replay = MemTable()
        if os.path.exists(log_path):
            with open(log_path, "rb") as f:
                for record in LogReader(f, paranoid=self._options.paranoid_checks):
                    batch = WriteBatch.from_bytes(record)
                    insert_into_memtable(batch, replay)
                    last = batch.sequence + batch.count - 1
                    max_sequence = max(max_sequence, last)
        self._vset.last_sequence = max_sequence

        # Roll a fresh log + manifest for the newly opened session.
        new_log_number = self._vset.new_file_number()
        self._vset.log_number = new_log_number
        self._vset.create_new_manifest()
        self._open_log(new_log_number)

        # Persist anything we replayed as a level-0 table so the old log is
        # genuinely obsolete.
        if not replay.empty():
            self._write_level0(replay)
            self._maybe_compact()
        else:
            self._mem = MemTable()

    def _open_log(self, number: int) -> None:
        if self._log_writer is not None:
            self._log_writer.close()
        self._log_file = open(log_file_name(self._name, number), "wb")
        self._log_writer = LogWriter(self._log_file)

    # ----------------------------------------------------------- write path #
    def put(self, key: bytes, value: bytes, options: WriteOptions | None = None) -> None:
        batch = WriteBatch()
        batch.put(key, value)
        self.write(batch, options)

    def delete(self, key: bytes, options: WriteOptions | None = None) -> None:
        batch = WriteBatch()
        batch.delete(key)
        self.write(batch, options)

    def write(self, batch: WriteBatch, options: WriteOptions | None = None) -> None:
        options = options or DEFAULT_WRITE_OPTIONS
        if batch.count == 0:
            return
        with self._lock:
            self._ensure_open()
            self._make_room_for_write()
            sequence = self._vset.last_sequence + 1
            batch.sequence = sequence
            self._log_writer.add_record(batch.encode(), sync=options.sync)
            insert_into_memtable(batch, self._mem)
            self._vset.last_sequence = sequence + batch.count - 1

    def _make_room_for_write(self) -> None:
        if self._mem.approximate_memory_usage() >= self._options.write_buffer_size:
            frozen = self._mem
            new_log_number = self._vset.new_file_number()
            self._open_log(new_log_number)
            self._vset.log_number = new_log_number
            self._mem = MemTable()
            self._write_level0(frozen, log_number=new_log_number)
            self._maybe_compact()
            self._delete_obsolete_files()

    def _write_level0(self, memtable: MemTable, log_number: int | None = None) -> None:
        number = self._vset.new_file_number()
        meta = flush_memtable(self._name, self._options, number, memtable)
        edit = VersionEdit()
        edit.add_file(0, meta)
        if log_number is not None:
            edit.log_number = log_number
        self._vset.log_and_apply(edit)
        if self._mem is memtable:
            self._mem = MemTable()

    def _maybe_compact(self) -> None:
        for _ in range(_MAX_COMPACTIONS_PER_DRAIN):
            compaction = pick_compaction(self._vset.current, self._options)
            if compaction is None:
                return
            smallest_snapshot = self._smallest_snapshot()
            edit = run_compaction(
                self._name,
                self._options,
                self._vset.current,
                self._table_cache,
                compaction,
                smallest_snapshot,
                self._vset.new_file_number,
            )
            self._vset.log_and_apply(edit)
            for _level, number in edit.deleted_files:
                self._table_cache.evict(number)
            self._delete_obsolete_files()

    # ------------------------------------------------------------ read path #
    def get(
        self,
        key: bytes,
        default: bytes | None = None,
        options: ReadOptions | None = None,
    ) -> bytes | None:
        options = options or DEFAULT_READ_OPTIONS
        with self._lock:
            self._ensure_open()
            sequence = (
                options.snapshot.sequence
                if options.snapshot is not None
                else self._vset.last_sequence
            )
            status, value = self._mem.get(key, sequence)
            if status == LookupResult.FOUND:
                return value
            if status == LookupResult.DELETED:
                return default
            status, value = self._vset.current.get(key, sequence, self._table_cache)
            if status == LookupResult.FOUND:
                return value
            return default

    def __getitem__(self, key: bytes) -> bytes:
        sentinel = object()
        value = self.get(key, default=sentinel)  # type: ignore[arg-type]
        if value is sentinel:
            raise KeyError(key)
        return value  # type: ignore[return-value]

    def __setitem__(self, key: bytes, value: bytes) -> None:
        self.put(key, value)

    def __delitem__(self, key: bytes) -> None:
        self.delete(key)

    def __contains__(self, key: bytes) -> bool:
        sentinel = object()
        return self.get(key, default=sentinel) is not sentinel  # type: ignore[arg-type]

    def scan(self, start: bytes | None = None, end: bytes | None = None, *, snapshot=None):
        """Iterate ``(key, value)`` pairs with ``start <= key < end`` in order.

        Returns a closeable generator; both bounds are optional. The view is
        consistent as of ``snapshot`` (or the latest state when omitted).
        """
        with self._lock:
            self._ensure_open()
            sequence = snapshot.sequence if snapshot is not None else self._vset.last_sequence
            children = [self._mem.iterator()]
            readers: list[SSTableReader] = []
            version = self._vset.current
            for level in range(version.num_levels):
                for meta in version.files[level]:
                    reader = SSTableReader.open(
                        table_file_name(self._name, meta.number), self._options
                    )
                    readers.append(reader)
                    children.append(reader.iterator())

        def generate():
            try:
                yield from DBIterator(MergingIterator(children), sequence, start, end)
            finally:
                for reader in readers:
                    reader.close()

        return generate()

    def items(self, **kwargs):
        """Alias for :meth:`scan` reading the convenience way."""
        return self.scan(**kwargs)

    # ------------------------------------------------------------ snapshots #
    def snapshot(self) -> Snapshot:
        with self._lock:
            self._ensure_open()
            snap = Snapshot(self._vset.last_sequence, self)
            self._snapshots.add(snap)
            return snap

    def release_snapshot(self, snap: Snapshot) -> None:
        with self._lock:
            self._snapshots.discard(snap)

    def _smallest_snapshot(self) -> int:
        if self._snapshots:
            return min(s.sequence for s in self._snapshots)
        return self._vset.last_sequence

    # ---------------------------------------------------------- maintenance #
    def flush(self) -> None:
        """Force the active memtable out to a level-0 table."""
        with self._lock:
            self._ensure_open()
            if self._mem.empty():
                return
            frozen = self._mem
            new_log_number = self._vset.new_file_number()
            self._open_log(new_log_number)
            self._vset.log_number = new_log_number
            self._mem = MemTable()
            self._write_level0(frozen, log_number=new_log_number)
            self._delete_obsolete_files()

    def compact(self) -> None:
        """Flush, then drain all pending compactions."""
        with self._lock:
            self._ensure_open()
            if not self._mem.empty():
                self.flush()
            self._maybe_compact()

    def stats(self) -> dict:
        with self._lock:
            self._ensure_open()
            version = self._vset.current
            return {
                "last_sequence": self._vset.last_sequence,
                "memtable_bytes": self._mem.approximate_memory_usage(),
                "memtable_entries": len(self._mem),
                "num_files": sum(version.num_files(i) for i in range(version.num_levels)),
                "total_bytes": sum(
                    version.total_file_size(i) for i in range(version.num_levels)
                ),
                "levels": version.level_summary(),
                "live_snapshots": len(self._snapshots),
            }

    # --------------------------------------------------------- housekeeping #
    def _delete_obsolete_files(self) -> None:
        version = self._vset.current
        live_tables = {
            meta.number
            for level in range(version.num_levels)
            for meta in version.files[level]
        }
        for entry in os.listdir(self._name):
            parsed = parse_file_name(entry)
            if parsed is None:
                continue
            number, kind = parsed
            keep = True
            if kind == "log":
                keep = number >= self._vset.log_number
            elif kind == "table":
                keep = number in live_tables
            elif kind == "manifest":
                keep = number == self._vset.manifest_file_number
            elif kind == "temp":
                keep = False
            if not keep:
                try:
                    os.remove(os.path.join(self._name, entry))
                except OSError:
                    pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise DBClosed("database is closed")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._log_writer is not None:
                self._log_writer.close()
                self._log_writer = None
            self._table_cache.close()
            self._vset.close()
            self._release_lock()

    def __enter__(self) -> DB:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["DB", "Snapshot"]
