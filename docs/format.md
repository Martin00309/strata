# Strata on-disk format (v1)

This document specifies the byte layout Strata writes. It is a compact,
LevelDB-inspired format; the goal here is that a reader could reconstruct a
store, or a debugger could make sense of the bytes, without reading the code.

All integers are little-endian. Two integer encodings are used:

- **fixed32 / fixed64** — 4- or 8-byte little-endian unsigned integers.
- **varint** — unsigned LEB128: 7 bits per byte, high bit set on all but the
  last byte.

A **length-prefixed** byte string is a `varint` length followed by that many
bytes.

## Database directory

A database is a directory containing:

| File | Purpose |
| --- | --- |
| `CURRENT` | one text line naming the live `MANIFEST-*` file |
| `MANIFEST-NNNNNN` | log of version edits (the metadata journal) |
| `NNNNNN.log` | write-ahead log |
| `NNNNNN.sst` | immutable sorted table |
| `LOCK` | held (advisory `flock`) while the database is open |

`NNNNNN` is a zero-padded file number drawn from a single monotonic counter, so
a number is unique across all file kinds.

## Internal keys

A user key is stored with an 8-byte trailer:

```
internal_key = user_key || fixed64( (sequence << 8) | value_type )
```

`value_type` is `0` for a deletion tombstone and `1` for a value. Internal keys
order by user key ascending, then by the trailer **descending**, so the newest
version of a key (largest sequence) sorts first.

## Write-ahead log and manifest records

Both the WAL and the manifest are record logs with identical framing:

```
record = fixed32(masked_crc(payload)) || fixed32(len(payload)) || payload
```

`masked_crc` is a CRC-32 of the payload, rotated and biased (LevelDB's mask) so
a stored CRC never coincides with the data it covers. A record whose header or
payload is truncated is treated as a crash-torn tail and ends recovery; a CRC
mismatch ends recovery too (and raises under `paranoid_checks`).

### WAL payload: a write batch

```
batch   = fixed64(sequence) || fixed32(count) || op*count
op      = u8(type) || key:len-prefixed || [ value:len-prefixed if type == 1 ]
```

`sequence` is the sequence number assigned to the batch's first operation; the
i-th operation gets `sequence + i`.

### Manifest payload: a version edit

A version edit is a series of tagged fields (`varint` tag + body):

| Tag | Field | Body |
| --- | --- | --- |
| 1 | comparator name | length-prefixed string |
| 2 | log number | varint |
| 3 | next file number | varint |
| 4 | last sequence | varint |
| 5 | deleted file | varint level, varint number |
| 6 | new file | varint level, varint number, varint size, smallest:len-prefixed, largest:len-prefixed |

`smallest`/`largest` are the file's smallest and largest **internal** keys.
Replaying every edit in the manifest in order rebuilds the current set of files
per level.

## Blocks

Data blocks and the index block share one format: sorted entries with prefix
compression, plus restart points for binary search.

```
entry      = varint(shared) varint(non_shared) varint(value_len)
             key[shared:]    value
trailer    = fixed32(restart_offset)*num_restarts || fixed32(num_restarts)
```

`shared` is the number of leading bytes this key shares with the previous key.
Every `block_restart_interval` entries a key is stored in full (`shared == 0`)
and its offset recorded in the restart array, so a seek binary-searches the
restart points and then scans forward.

On disk, every block is followed by a 5-byte block trailer:

```
block_on_disk = block_bytes || u8(compression_type) || fixed32(masked_crc(block_bytes || compression_type))
```

`compression_type` is `0` (none) in v1. A **block handle** is
`varint(offset) || varint(size)`, where `size` is the block length excluding the
trailer.

## SSTable

```
[ data block 0 ][trailer]
[ data block 1 ][trailer]
...
[ filter block ][trailer]      # Bloom filter over all user keys
[ index  block ][trailer]      # last key of each data block -> block handle
[ footer: 40 bytes ]
```

The **index block** maps the last key of each data block to that block's
handle. The **filter block** is the serialised Bloom filter (empty when
disabled). The **footer** is fixed at 40 bytes:

```
footer = fixed64(filter_offset) fixed64(filter_size)
         fixed64(index_offset)  fixed64(index_size)
         fixed64(magic = 0x5354524154410001)
```

### Bloom filter

For `n` keys at `bits_per_key` bits, the filter is `ceil(max(64, n*bits_per_key)
/ 8)` bytes of bit array followed by one byte holding the probe count
`k = round(bits_per_key * ln 2)` (clamped to 1..30). Each key sets `k` bits
using double hashing (`h`, then `h += rotate_right(h, 17)`), with the same hash
function as LevelDB. A trailing byte `> 30` is treated as an unknown encoding
and matches everything.

## Compatibility

The format is versioned by the table magic and by the manifest's comparator
name, which is checked on open. v1 is the only format so far; future changes
will either stay backward-readable or document the break.
