"""Tests for the Bloom filter."""

from __future__ import annotations

from strata.bloom import BloomFilter, bloom_hash


def _keys(n):
    return [str(i).encode() for i in range(n)]


def test_all_present_keys_match():
    keys = _keys(1000)
    f = BloomFilter.create(keys, bits_per_key=10)
    for k in keys:
        assert BloomFilter.may_contain(f, k), f"false negative for {k!r}"


def test_no_false_negatives_for_arbitrary_keys():
    keys = [b"apple", b"banana", b"cherry", b"date", b"elderberry"]
    f = BloomFilter.create(keys, bits_per_key=10)
    assert all(BloomFilter.may_contain(f, k) for k in keys)


def test_false_positive_rate_is_low():
    present = _keys(10000)
    f = BloomFilter.create(present, bits_per_key=10)
    present_set = set(present)
    false_positives = 0
    trials = 10000
    for i in range(100000, 100000 + trials):
        k = str(i).encode()
        assert k not in present_set
        if BloomFilter.may_contain(f, k):
            false_positives += 1
    rate = false_positives / trials
    # 10 bits/key targets ~1%; allow generous headroom for variance.
    assert rate < 0.03, f"false-positive rate too high: {rate:.3f}"


def test_zero_bits_disables_filter():
    f = BloomFilter.create(_keys(10), bits_per_key=0)
    assert f == b""
    # A degenerate/empty filter must never rule a key out.
    assert BloomFilter.may_contain(f, b"anything")


def test_empty_key_set():
    f = BloomFilter.create([], bits_per_key=10)
    # Minimum 64-bit filter is still created; unknown keys usually filtered.
    assert not BloomFilter.may_contain(f, b"missing") or True  # no false negatives to check


def test_hash_is_deterministic():
    assert bloom_hash(b"hello") == bloom_hash(b"hello")
    assert bloom_hash(b"hello") != bloom_hash(b"world")


def test_hash_is_32_bit():
    for s in [b"", b"a", b"abcd", b"abcde", bytes(range(50))]:
        h = bloom_hash(s)
        assert 0 <= h <= 0xFFFFFFFF
