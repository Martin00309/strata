"""Low-level binary encoding helpers.

Everything Strata writes to disk — log records, table blocks, the manifest —
is built from the same handful of primitives defined here:

* fixed-width little-endian 32- and 64-bit integers,
* LEB128-style variable-length integers (varints),
* length-prefixed byte strings, and
* a masked CRC-32 for integrity checks.

The functions come in two flavours: ``put_*`` append to a ``bytearray`` and
``get_*`` read from a ``bytes``/``memoryview`` at an offset and return the new
offset. Keeping the cursor explicit (rather than slicing) avoids a lot of
allocation on hot paths.
"""

from __future__ import annotations

import struct
import zlib

_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")

# Largest value representable by a 32-bit varint field. Used for validation.
_VARINT32_MAX = (1 << 32) - 1


# --------------------------------------------------------------------------- #
# Fixed-width integers
# --------------------------------------------------------------------------- #
def encode_fixed32(value: int) -> bytes:
    return _U32.pack(value)


def encode_fixed64(value: int) -> bytes:
    return _U64.pack(value)


def put_fixed32(buf: bytearray, value: int) -> None:
    buf += _U32.pack(value)


def put_fixed64(buf: bytearray, value: int) -> None:
    buf += _U64.pack(value)


def decode_fixed32(data: bytes, offset: int = 0) -> int:
    return _U32.unpack_from(data, offset)[0]


def decode_fixed64(data: bytes, offset: int = 0) -> int:
    return _U64.unpack_from(data, offset)[0]


# --------------------------------------------------------------------------- #
# Varints (unsigned LEB128)
# --------------------------------------------------------------------------- #
def put_varint(buf: bytearray, value: int) -> None:
    """Append ``value`` (a non-negative int) to ``buf`` as a varint."""
    if value < 0:
        raise ValueError("varint cannot encode a negative value")
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            buf.append(byte | 0x80)
        else:
            buf.append(byte)
            return


def encode_varint(value: int) -> bytes:
    buf = bytearray()
    put_varint(buf, value)
    return bytes(buf)


def get_varint(data, offset: int = 0) -> tuple[int, int]:
    """Decode a varint from ``data`` at ``offset``.

    Returns ``(value, new_offset)``. Raises :class:`ValueError` if the buffer
    ends mid-varint or the varint is unreasonably long (> 10 bytes).
    """
    result = 0
    shift = 0
    pos = offset
    n = len(data)
    while True:
        if pos >= n:
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift >= 70:
            raise ValueError("varint is too long")


def varint_length(value: int) -> int:
    """Number of bytes :func:`put_varint` would emit for ``value``."""
    if value < 0:
        raise ValueError("varint cannot encode a negative value")
    length = 1
    while value >= 0x80:
        value >>= 7
        length += 1
    return length


# --------------------------------------------------------------------------- #
# Length-prefixed byte strings
# --------------------------------------------------------------------------- #
def put_length_prefixed(buf: bytearray, data: bytes) -> None:
    put_varint(buf, len(data))
    buf += data


def get_length_prefixed(data, offset: int = 0) -> tuple[bytes, int]:
    length, pos = get_varint(data, offset)
    end = pos + length
    if end > len(data):
        raise ValueError("length-prefixed slice overruns buffer")
    return bytes(data[pos:end]), end


# --------------------------------------------------------------------------- #
# CRC-32 (masked, à la LevelDB)
# --------------------------------------------------------------------------- #
_MASK_DELTA = 0xA282EAD8


def crc32(data: bytes) -> int:
    """Unmasked CRC-32 of ``data`` (zlib's polynomial)."""
    return zlib.crc32(data) & 0xFFFFFFFF


def mask_crc(crc: int) -> int:
    """Rotate and bias a CRC so a CRC never appears in the data it covers.

    This mirrors LevelDB's masking: storing a raw CRC directly adjacent to the
    bytes it protects defeats CRC's own guarantees, so it is rotated right 15
    bits and offset by a constant before being written.
    """
    return (((crc >> 15) | (crc << 17)) + _MASK_DELTA) & 0xFFFFFFFF


def unmask_crc(masked: int) -> int:
    rot = (masked - _MASK_DELTA) & 0xFFFFFFFF
    return ((rot >> 17) | (rot << 15)) & 0xFFFFFFFF


def masked_crc32(data: bytes) -> int:
    """Masked CRC-32 of ``data`` — what actually gets written to disk."""
    return mask_crc(crc32(data))


__all__ = [
    "encode_fixed32",
    "encode_fixed64",
    "put_fixed32",
    "put_fixed64",
    "decode_fixed32",
    "decode_fixed64",
    "put_varint",
    "encode_varint",
    "get_varint",
    "varint_length",
    "put_length_prefixed",
    "get_length_prefixed",
    "crc32",
    "mask_crc",
    "unmask_crc",
    "masked_crc32",
]
