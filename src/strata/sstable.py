"""Sorted String Tables (SSTables) — immutable on-disk sorted runs.

When a memtable fills up, or when compaction merges tables, the result is
written as an SSTable: a sequence of data blocks, a Bloom filter over the user
keys, an index block, and a fixed-size footer that points at the other two::

    [ data block 0 ][trailer]
    [ data block 1 ][trailer]
    ...
    [ filter block ][trailer]      # Bloom filter over all user keys
    [ index  block ][trailer]      # last key of each data block -> block handle
    [ footer: 40 bytes ]           # filter handle, index handle, magic

Every block is followed by a 5-byte trailer (1 compression byte + 4 CRC bytes)
so reads can detect bit-rot. The index block lets a point lookup find the one
data block that could hold a key with a single binary search; the Bloom filter
lets most *absent* lookups skip the data block read entirely.

Tables are immutable once written, which is what makes concurrent reads and
snapshots cheap: a reader never has to worry about a table changing underneath
it.
"""

from __future__ import annotations

from collections import OrderedDict

from .block import Block, BlockBuilder
from .bloom import BloomFilter
from .coding import (
    decode_fixed32,
    decode_fixed64,
    encode_fixed64,
    encode_varint,
    get_varint,
    masked_crc32,
    put_fixed32,
)
from .errors import Corruption
from .internalkey import (
    TYPE_VALUE,
    compare_internal_keys,
    extract_user_key,
    make_lookup_key,
    parse_internal_key,
)
from .memtable import LookupResult
from .options import Options

# "STRATAv1" little-endian-ish; just needs to be a stable sentinel.
TABLE_MAGIC = 0x53_54_52_41_54_41_00_01
FOOTER_SIZE = 40  # 2 block handles (16 each) + 8-byte magic
_BLOCK_TRAILER_SIZE = 5  # 1 compression byte + fixed32 crc
_NO_COMPRESSION = 0


def _encode_handle(offset: int, size: int) -> bytes:
    return encode_varint(offset) + encode_varint(size)


def _decode_handle(data: bytes, pos: int) -> tuple[int, int, int]:
    offset, pos = get_varint(data, pos)
    size, pos = get_varint(data, pos)
    return offset, size, pos


class TableMeta:
    """Metadata produced by writing a table — fed into the manifest."""

    __slots__ = ("smallest", "largest", "num_entries", "file_size")

    def __init__(self, smallest, largest, num_entries, file_size):
        self.smallest = smallest
        self.largest = largest
        self.num_entries = num_entries
        self.file_size = file_size


class SSTableWriter:
    """Writes entries, supplied in internal-key order, to a table file."""

    def __init__(self, fileobj, options: Options):
        self._file = fileobj
        self._options = options
        self._data_block = BlockBuilder(options.block_restart_interval)
        # The index is searched one entry at a time, so index every entry.
        self._index_block = BlockBuilder(1)
        self._offset = 0
        self._num_entries = 0
        self._user_keys: list[bytes] = []
        self._smallest: bytes | None = None
        self._largest: bytes | None = None
        self._last_key: bytes | None = None
        self._pending_index_key: bytes | None = None
        self._finished = False

    def add(self, internal_key: bytes, value: bytes) -> None:
        assert not self._finished, "add() after finish()"
        if self._last_key is not None:
            assert compare_internal_keys(self._last_key, internal_key) < 0, (
                "SSTable entries must be added in strictly increasing order"
            )
        if self._smallest is None:
            self._smallest = internal_key
        self._largest = internal_key
        self._user_keys.append(extract_user_key(internal_key))

        self._data_block.add(internal_key, value)
        self._last_key = internal_key
        self._num_entries += 1

        if self._data_block.current_size_estimate() >= self._options.block_size:
            self._flush_data_block()

    def _flush_data_block(self) -> None:
        if self._data_block.empty():
            return
        content = self._data_block.finish()
        handle = self._write_raw(content)
        # The last key of the block is a valid separator: it is >= every key in
        # the block and < the first key of the next block (keys are unique).
        self._index_block.add(self._last_key, _encode_handle(*handle))
        self._data_block.reset()

    def _write_raw(self, content: bytes) -> tuple[int, int]:
        """Write a block + trailer; return its ``(offset, size)`` handle."""
        offset = self._offset
        trailer = bytearray()
        trailer.append(_NO_COMPRESSION)
        crc = masked_crc32(content + bytes([_NO_COMPRESSION]))
        put_fixed32(trailer, crc)
        self._file.write(content)
        self._file.write(trailer)
        self._offset += len(content) + _BLOCK_TRAILER_SIZE
        return offset, len(content)

    def finish(self) -> TableMeta:
        self._flush_data_block()

        # Filter block (Bloom over every user key). Empty when disabled.
        filter_bytes = BloomFilter.create(self._user_keys, self._options.bloom_bits_per_key)
        filter_handle = self._write_raw(filter_bytes)

        index_content = self._index_block.finish()
        index_handle = self._write_raw(index_content)

        footer = bytearray()
        footer += encode_fixed64(filter_handle[0])
        footer += encode_fixed64(filter_handle[1])
        footer += encode_fixed64(index_handle[0])
        footer += encode_fixed64(index_handle[1])
        footer += encode_fixed64(TABLE_MAGIC)
        assert len(footer) == FOOTER_SIZE
        self._file.write(footer)
        self._offset += FOOTER_SIZE

        self._finished = True
        return TableMeta(self._smallest, self._largest, self._num_entries, self._offset)


class SSTableReader:
    """Reads a table file written by :class:`SSTableWriter`."""

    def __init__(self, fileobj, options: Options, read_verify: bool = False):
        self._file = fileobj
        self._options = options
        self._verify = read_verify or options.paranoid_checks
        self._block_cache: OrderedDict[int, bytes] = OrderedDict()
        self._block_cache_cap = 64

        file_size = self._file.seek(0, 2)
        if file_size < FOOTER_SIZE:
            raise Corruption("table file smaller than its footer")
        self._file.seek(file_size - FOOTER_SIZE)
        footer = self._file.read(FOOTER_SIZE)
        magic = decode_fixed64(footer, 32)
        if magic != TABLE_MAGIC:
            raise Corruption("bad table magic; not a Strata table or truncated")
        filter_off = decode_fixed64(footer, 0)
        filter_size = decode_fixed64(footer, 8)
        index_off = decode_fixed64(footer, 16)
        index_size = decode_fixed64(footer, 24)

        self._filter = self._read_raw(filter_off, filter_size)
        index_bytes = self._read_raw(index_off, index_size)
        self._index = Block(index_bytes, compare_internal_keys)

    @classmethod
    def open(cls, path: str, options: Options, read_verify: bool = False) -> SSTableReader:
        return cls(open(path, "rb"), options, read_verify)

    def close(self) -> None:
        self._block_cache.clear()
        self._file.close()

    def _read_raw(self, offset: int, size: int) -> bytes:
        self._file.seek(offset)
        content = self._file.read(size)
        trailer = self._file.read(_BLOCK_TRAILER_SIZE)
        if len(content) < size or len(trailer) < _BLOCK_TRAILER_SIZE:
            raise Corruption("short read of table block")
        if self._verify:
            comp = trailer[0]
            stored = decode_fixed32(trailer, 1)
            if masked_crc32(content + bytes([comp])) != stored:
                raise Corruption("table block failed CRC check")
        return content

    def _load_block(self, handle_value: bytes) -> Block:
        offset, size, _ = _decode_handle(handle_value, 0)
        if offset in self._block_cache:
            self._block_cache.move_to_end(offset)
            content = self._block_cache[offset]
        else:
            content = self._read_raw(offset, size)
            if self._options.max_open_files > 0:
                self._block_cache[offset] = content
                if len(self._block_cache) > self._block_cache_cap:
                    self._block_cache.popitem(last=False)
        return Block(content, compare_internal_keys)

    def get(self, user_key: bytes, sequence: int) -> tuple[str, bytes | None]:
        """Point lookup for ``user_key`` as of ``sequence``."""
        if self._filter and not BloomFilter.may_contain(self._filter, user_key):
            return LookupResult.ABSENT, None

        lookup = make_lookup_key(user_key, sequence)
        index_it = self._index.iterator()
        index_it.seek(lookup)
        if not index_it.valid():
            return LookupResult.ABSENT, None

        block = self._load_block(index_it.value())
        data_it = block.iterator()
        data_it.seek(lookup)
        if not data_it.valid():
            return LookupResult.ABSENT, None

        found_user, _seq, vtype = parse_internal_key(data_it.key())
        if found_user != user_key:
            return LookupResult.ABSENT, None
        if vtype == TYPE_VALUE:
            return LookupResult.FOUND, data_it.value()
        return LookupResult.DELETED, None

    def iterator(self) -> TableIterator:
        return TableIterator(self)

    # Used by the two-level iterator.
    def _index_iterator(self):
        return self._index.iterator()


class TableIterator:
    """A two-level cursor: walk the index, and within each block the entries.

    Exposes the engine-wide iterator contract (``internal_key`` / ``value`` /
    ``valid`` / ``seek`` / ``seek_to_first`` / ``next``) so it can be merged
    with memtable iterators and other tables.
    """

    def __init__(self, reader: SSTableReader):
        self._reader = reader
        self._index_it = reader._index_iterator()
        self._data_it = None

    def valid(self) -> bool:
        return self._data_it is not None and self._data_it.valid()

    def internal_key(self) -> bytes:
        return self._data_it.key()

    def key(self) -> bytes:
        return extract_user_key(self._data_it.key())

    def value(self) -> bytes:
        return self._data_it.value()

    def _load_current_block(self) -> None:
        block = self._reader._load_block(self._index_it.value())
        self._data_it = block.iterator()

    def seek_to_first(self) -> None:
        self._index_it.seek_to_first()
        if not self._index_it.valid():
            self._data_it = None
            return
        self._load_current_block()
        self._data_it.seek_to_first()
        self._skip_empty_forward()

    def seek(self, internal_target: bytes) -> None:
        self._index_it.seek(internal_target)
        if not self._index_it.valid():
            self._data_it = None
            return
        self._load_current_block()
        self._data_it.seek(internal_target)
        self._skip_empty_forward()

    def next(self) -> None:
        assert self.valid(), "next() on invalid iterator"
        self._data_it.next()
        self._skip_empty_forward()

    def _skip_empty_forward(self) -> None:
        """Advance across block boundaries until positioned on a live entry."""
        while self._data_it is None or not self._data_it.valid():
            if not self._index_it.valid():
                self._data_it = None
                return
            self._index_it.next()
            if not self._index_it.valid():
                self._data_it = None
                return
            self._load_current_block()
            self._data_it.seek_to_first()


__all__ = ["SSTableWriter", "SSTableReader", "TableIterator", "TableMeta", "TABLE_MAGIC"]
