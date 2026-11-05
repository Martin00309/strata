"""Tests for the binary encoding primitives."""

from __future__ import annotations

import pytest

from strata import coding


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 300, 16384, 2**31, 2**32 - 1, 2**53])
def test_varint_roundtrip(value):
    encoded = coding.encode_varint(value)
    decoded, offset = coding.get_varint(encoded, 0)
    assert decoded == value
    assert offset == len(encoded)
    assert offset == coding.varint_length(value)


def test_varint_lengths_match_spec():
    assert coding.varint_length(0) == 1
    assert coding.varint_length(127) == 1
    assert coding.varint_length(128) == 2
    assert coding.varint_length(16383) == 2
    assert coding.varint_length(16384) == 3


def test_varint_sequence_in_buffer():
    buf = bytearray()
    values = [1, 500, 0, 2**20, 7]
    for v in values:
        coding.put_varint(buf, v)
    offset = 0
    out = []
    for _ in values:
        v, offset = coding.get_varint(buf, offset)
        out.append(v)
    assert out == values
    assert offset == len(buf)


def test_varint_rejects_negative():
    with pytest.raises(ValueError):
        coding.encode_varint(-1)


def test_varint_truncated_raises():
    # 0x80 alone promises a continuation byte that never comes.
    with pytest.raises(ValueError):
        coding.get_varint(b"\x80", 0)


def test_fixed32_and_64_roundtrip():
    assert coding.decode_fixed32(coding.encode_fixed32(0xDEADBEEF)) == 0xDEADBEEF
    assert coding.decode_fixed64(coding.encode_fixed64(0x0123456789ABCDEF)) == 0x0123456789ABCDEF


def test_fixed_are_little_endian():
    assert coding.encode_fixed32(1) == b"\x01\x00\x00\x00"
    assert coding.encode_fixed64(1) == b"\x01\x00\x00\x00\x00\x00\x00\x00"


def test_length_prefixed_roundtrip():
    buf = bytearray()
    coding.put_length_prefixed(buf, b"hello")
    coding.put_length_prefixed(buf, b"")
    coding.put_length_prefixed(buf, b"\x00\xff\x10")
    offset = 0
    a, offset = coding.get_length_prefixed(buf, offset)
    b, offset = coding.get_length_prefixed(buf, offset)
    c, offset = coding.get_length_prefixed(buf, offset)
    assert (a, b, c) == (b"hello", b"", b"\x00\xff\x10")
    assert offset == len(buf)


def test_length_prefixed_overrun_raises():
    # Declares length 5 but only 2 bytes follow.
    bad = coding.encode_varint(5) + b"ab"
    with pytest.raises(ValueError):
        coding.get_length_prefixed(bad, 0)


def test_crc_mask_roundtrip():
    for data in [b"", b"a", b"the quick brown fox", bytes(range(256))]:
        crc = coding.crc32(data)
        masked = coding.mask_crc(crc)
        assert coding.unmask_crc(masked) == crc
        # Masking must actually change the value (that's the point).
        if data:
            assert masked != crc


def test_masked_crc_detects_corruption():
    data = b"important record payload"
    good = coding.masked_crc32(data)
    corrupt = bytearray(data)
    corrupt[0] ^= 0x01
    assert coding.masked_crc32(bytes(corrupt)) != good
