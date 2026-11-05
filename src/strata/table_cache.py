"""An LRU cache of open SSTable readers.

Opening a table parses its footer and index block, so reopening it on every
read would be wasteful. The table cache keeps the hottest tables open, bounded
by ``max_open_files``, and closes the least-recently-used reader when the cap is
exceeded. It is the single place that turns a file *number* into a live reader,
which also makes eviction-on-delete (after compaction) straightforward.
"""

from __future__ import annotations

from collections import OrderedDict

from .filenames import table_file_name
from .options import Options
from .sstable import SSTableReader


class TableCache:
    def __init__(self, dbname: str, options: Options, capacity: int | None = None):
        self._dbname = dbname
        self._options = options
        self._capacity = capacity if capacity is not None else max(16, options.max_open_files)
        self._readers: OrderedDict[int, SSTableReader] = OrderedDict()

    def _reader(self, number: int) -> SSTableReader:
        reader = self._readers.get(number)
        if reader is not None:
            self._readers.move_to_end(number)
            return reader
        path = table_file_name(self._dbname, number)
        reader = SSTableReader.open(path, self._options)
        self._readers[number] = reader
        self._evict_if_needed()
        return reader

    def _evict_if_needed(self) -> None:
        while len(self._readers) > self._capacity:
            _number, reader = self._readers.popitem(last=False)
            reader.close()

    def get(self, number: int, user_key: bytes, sequence: int):
        """Point lookup in table ``number``."""
        return self._reader(number).get(user_key, sequence)

    def new_iterator(self, number: int):
        return self._reader(number).iterator()

    def evict(self, number: int) -> None:
        reader = self._readers.pop(number, None)
        if reader is not None:
            reader.close()

    def close(self) -> None:
        for reader in self._readers.values():
            reader.close()
        self._readers.clear()


__all__ = ["TableCache"]
