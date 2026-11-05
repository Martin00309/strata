"""Internal keys: how Strata encodes multi-version ordering.

A user stores plain ``bytes`` keys, but internally every write is tagged with a
monotonically increasing *sequence number* and a *value type* (a real value or
a deletion tombstone). The three are packed into an **internal key**::

    internal_key = user_key + fixed64(  (sequence << 8) | value_type  )

The 8-byte trailer is little-endian. Internal keys sort by:

1. user key, ascending (plain ``bytes`` comparison), then
2. the trailer, *descending* — so for a single user key the newest version
   (largest sequence) sorts first.

That ordering is the whole trick behind reads: to find the value of ``k`` as of
sequence ``s``, seek to the first internal key ``>= k⌢(s)`` and the first entry
you land on is the newest version of ``k`` not newer than ``s``.
"""

from __future__ import annotations

from .coding import decode_fixed64, encode_fixed64

# Value types stored in the low byte of the trailer.
TYPE_DELETION = 0x0
TYPE_VALUE = 0x1

# When building a *lookup* key we want it to sort at-or-before every real entry
# for the same (user_key, sequence), regardless of that entry's type. 0xFF is
# larger than any real type, and because the trailer sorts descending it makes
# the lookup key the smallest internal key at that sequence.
TYPE_FOR_SEEK = 0xFF

# Sequence numbers occupy the upper 56 bits of the 64-bit trailer.
MAX_SEQUENCE = (1 << 56) - 1


def pack_trailer(sequence: int, value_type: int) -> int:
    if sequence < 0 or sequence > MAX_SEQUENCE:
        raise ValueError(f"sequence out of range: {sequence}")
    if value_type < 0 or value_type > 0xFF:
        raise ValueError(f"value_type out of range: {value_type}")
    return (sequence << 8) | value_type


def unpack_trailer(trailer: int) -> tuple[int, int]:
    return trailer >> 8, trailer & 0xFF


def make_internal_key(user_key: bytes, sequence: int, value_type: int) -> bytes:
    """Build an internal key from its parts."""
    return bytes(user_key) + encode_fixed64(pack_trailer(sequence, value_type))


def make_lookup_key(user_key: bytes, sequence: int) -> bytes:
    """Internal key used to *seek* for ``user_key`` as of ``sequence``."""
    return make_internal_key(user_key, sequence, TYPE_FOR_SEEK)


def extract_user_key(internal_key: bytes) -> bytes:
    """The user-key portion of an internal key (everything but the trailer)."""
    if len(internal_key) < 8:
        raise ValueError("internal key shorter than its 8-byte trailer")
    return internal_key[:-8]


def extract_trailer(internal_key: bytes) -> int:
    if len(internal_key) < 8:
        raise ValueError("internal key shorter than its 8-byte trailer")
    return decode_fixed64(internal_key, len(internal_key) - 8)


def parse_internal_key(internal_key: bytes) -> tuple[bytes, int, int]:
    """Return ``(user_key, sequence, value_type)``."""
    trailer = extract_trailer(internal_key)
    sequence, value_type = unpack_trailer(trailer)
    return internal_key[:-8], sequence, value_type


def compare_internal_keys(a: bytes, b: bytes) -> int:
    """Three-way comparison implementing the internal-key ordering.

    Returns a negative/zero/positive int like ``(a > b) - (a < b)``.
    """
    ak = a[:-8]
    bk = b[:-8]
    if ak < bk:
        return -1
    if ak > bk:
        return 1
    # Same user key: larger trailer (newer) sorts first, i.e. compares *less*.
    at = decode_fixed64(a, len(a) - 8)
    bt = decode_fixed64(b, len(b) - 8)
    if at > bt:
        return -1
    if at < bt:
        return 1
    return 0


__all__ = [
    "TYPE_DELETION",
    "TYPE_VALUE",
    "TYPE_FOR_SEEK",
    "MAX_SEQUENCE",
    "pack_trailer",
    "unpack_trailer",
    "make_internal_key",
    "make_lookup_key",
    "extract_user_key",
    "extract_trailer",
    "parse_internal_key",
    "compare_internal_keys",
]
