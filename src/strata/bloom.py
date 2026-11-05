"""A Bloom filter for skipping tables that cannot contain a key.

Every on-disk table stores a Bloom filter over its user keys. On a point
lookup, the reader consults the filter first: if it says "no", the key is
definitely absent and an expensive block read is avoided; if it says "maybe",
the block is read. The false-positive rate is roughly ``0.6185 ** bits_per_key``
(about 1% at the default 10 bits).

The construction (double hashing with a rotated delta, ``k`` stored as the
final byte) and the hash function are byte-compatible with LevelDB's, which
keeps the on-disk format well specified and easy to cross-check.
"""

from __future__ import annotations

from .coding import decode_fixed32

_U32 = 0xFFFFFFFF
_SEED = 0xBC9F1D34
_M = 0xC6A4A793


def _hash(data: bytes, seed: int) -> int:
    """LevelDB's ``Hash`` — a MurmurHash-flavoured 32-bit hash."""
    n = len(data)
    h = (seed ^ ((n * _M) & _U32)) & _U32
    i = 0
    while n - i >= 4:
        w = decode_fixed32(data, i)
        i += 4
        h = (h + w) & _U32
        h = (h * _M) & _U32
        h ^= h >> 16
    remaining = n - i
    if remaining == 3:
        h = (h + (data[i + 2] << 16)) & _U32
        remaining = 2
    if remaining == 2:
        h = (h + (data[i + 1] << 8)) & _U32
        remaining = 1
    if remaining == 1:
        h = (h + data[i]) & _U32
        h = (h * _M) & _U32
        h ^= h >> 24
    return h & _U32


def bloom_hash(key: bytes) -> int:
    return _hash(key, _SEED)


def _probes(bits_per_key: int) -> int:
    # k = bits_per_key * ln(2); clamp to a sensible range.
    k = int(bits_per_key * 0.69)
    return max(1, min(30, k))


class BloomFilter:
    """Stateless builder/queries over the serialised filter byte string."""

    @staticmethod
    def create(keys: list[bytes], bits_per_key: int) -> bytes:
        """Build a filter covering ``keys``. Returns the serialised bytes."""
        if bits_per_key <= 0:
            return b""
        k = _probes(bits_per_key)
        n = len(keys)
        bits = max(64, n * bits_per_key)
        num_bytes = (bits + 7) // 8
        bits = num_bytes * 8
        array = bytearray(num_bytes)
        for key in keys:
            h = bloom_hash(key)
            delta = ((h >> 17) | (h << 15)) & _U32  # rotate right 17 bits
            for _ in range(k):
                bitpos = h % bits
                array[bitpos // 8] |= 1 << (bitpos % 8)
                h = (h + delta) & _U32
        array.append(k)  # stash probe count as the trailing byte
        return bytes(array)

    @staticmethod
    def may_contain(filter_bytes: bytes, key: bytes) -> bool:
        """Return ``False`` only if ``key`` is definitely not in the set."""
        length = len(filter_bytes)
        if length < 2:
            # Empty/degenerate filter: cannot rule anything out.
            return True
        k = filter_bytes[-1]
        if k > 30:
            # Reserved encoding we don't understand: don't filter anything out.
            return True
        bits = (length - 1) * 8
        h = bloom_hash(key)
        delta = ((h >> 17) | (h << 15)) & _U32
        for _ in range(k):
            bitpos = h % bits
            if not (filter_bytes[bitpos // 8] & (1 << (bitpos % 8))):
                return False
            h = (h + delta) & _U32
        return True


__all__ = ["BloomFilter", "bloom_hash"]
