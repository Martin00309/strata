"""Tests for the skip list."""

from __future__ import annotations

import random

from strata.skiplist import SkipList


def _bytes_cmp(a: bytes, b: bytes) -> int:
    return (a > b) - (a < b)


def test_insert_and_get():
    sl = SkipList(_bytes_cmp)
    sl.insert(b"b", b"2")
    sl.insert(b"a", b"1")
    sl.insert(b"c", b"3")
    assert sl.get(b"a") == b"1"
    assert sl.get(b"b") == b"2"
    assert sl.get(b"c") == b"3"
    assert sl.get(b"z") is None
    assert len(sl) == 3


def test_iteration_is_sorted():
    sl = SkipList(_bytes_cmp)
    keys = [b"delta", b"alpha", b"charlie", b"bravo", b"echo"]
    for k in keys:
        sl.insert(k, k.upper())
    assert [k for k, _ in sl] == sorted(keys)


def test_overwrite_updates_value_not_count():
    sl = SkipList(_bytes_cmp)
    sl.insert(b"k", b"old")
    sl.insert(b"k", b"new")
    assert sl.get(b"k") == b"new"
    assert len(sl) == 1


def test_contains():
    sl = SkipList(_bytes_cmp)
    sl.insert(b"present", b"x")
    assert sl.contains(b"present")
    assert not sl.contains(b"missing")


def test_iterator_seek():
    sl = SkipList(_bytes_cmp)
    for i in range(0, 100, 2):  # even keys 0,2,...,98
        sl.insert(f"{i:03d}".encode(), str(i).encode())
    it = sl.iterator()
    it.seek(b"050")
    assert it.valid() and it.key() == b"050"
    it.seek(b"051")  # no exact match -> next greater
    assert it.valid() and it.key() == b"052"
    it.seek(b"999")  # past the end
    assert not it.valid()
    it.seek_to_first()
    assert it.valid() and it.key() == b"000"


def test_randomised_against_sorted_dict():
    rng = random.Random(1234)
    sl = SkipList(_bytes_cmp)
    reference: dict[bytes, bytes] = {}
    for _ in range(2000):
        k = f"{rng.randrange(500):04d}".encode()
        v = f"{rng.randrange(1_000_000)}".encode()
        sl.insert(k, v)
        reference[k] = v
    assert len(sl) == len(reference)
    assert [k for k, _ in sl] == sorted(reference)
    for k, v in reference.items():
        assert sl.get(k) == v
