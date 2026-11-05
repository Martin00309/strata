"""Tests for the prefix-compressed block format."""

from __future__ import annotations

import random

from strata.block import Block, BlockBuilder


def _bytes_cmp(a: bytes, b: bytes) -> int:
    return (a > b) - (a < b)


def _build(pairs, restart_interval=16):
    builder = BlockBuilder(restart_interval)
    for k, v in pairs:
        builder.add(k, v)
    return Block(builder.finish(), _bytes_cmp)


def test_iterate_in_order():
    pairs = [(b"alpha", b"1"), (b"beta", b"2"), (b"gamma", b"3")]
    block = _build(pairs)
    assert list(block) == pairs


def test_prefix_compression_roundtrips():
    # Keys with long shared prefixes exercise the delta encoding.
    pairs = [
        (b"user:0001", b"a"),
        (b"user:0002", b"b"),
        (b"user:0003", b"c"),
        (b"user:0100", b"d"),
        (b"zebra", b"e"),
    ]
    block = _build(pairs, restart_interval=2)
    assert list(block) == pairs


def test_seek_exact_and_between():
    pairs = [(f"{i:03d}".encode(), str(i).encode()) for i in range(0, 100, 2)]
    block = _build(pairs, restart_interval=4)
    it = block.iterator()

    it.seek(b"050")
    assert it.valid() and it.key() == b"050" and it.value() == b"50"

    it.seek(b"051")  # falls to next greater
    assert it.valid() and it.key() == b"052"

    it.seek(b"000")  # first key
    assert it.valid() and it.key() == b"000"

    it.seek(b"999")  # past end
    assert not it.valid()


def test_seek_then_iterate_to_end():
    pairs = [(f"{i:03d}".encode(), str(i).encode()) for i in range(50)]
    block = _build(pairs, restart_interval=8)
    it = block.iterator()
    it.seek(b"040")
    collected = []
    while it.valid():
        collected.append(it.key())
        it.next()
    assert collected == [f"{i:03d}".encode() for i in range(40, 50)]


def test_single_entry_block():
    block = _build([(b"only", b"value")])
    assert list(block) == [(b"only", b"value")]
    it = block.iterator()
    it.seek(b"a")
    assert it.valid() and it.key() == b"only"
    it.seek(b"z")
    assert not it.valid()


def test_randomised_roundtrip_and_seek():
    rng = random.Random(7)
    keys = sorted({f"{rng.randrange(100000):08d}".encode() for _ in range(500)})
    pairs = [(k, k[::-1]) for k in keys]
    block = _build(pairs, restart_interval=16)
    assert list(block) == pairs
    it = block.iterator()
    for _ in range(200):
        target = f"{rng.randrange(100000):08d}".encode()
        it.seek(target)
        expected = next((k for k in keys if k >= target), None)
        if expected is None:
            assert not it.valid()
        else:
            assert it.valid() and it.key() == expected


def test_size_estimate_grows_with_entries():
    builder = BlockBuilder(16)
    assert builder.empty()
    base = builder.current_size_estimate()
    builder.add(b"k", b"v")
    assert not builder.empty()
    assert builder.current_size_estimate() > base
