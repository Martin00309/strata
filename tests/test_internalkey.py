"""Tests for internal-key encoding and the MVCC comparator."""

from __future__ import annotations

from functools import cmp_to_key

import pytest

from strata import internalkey as ik


def test_pack_unpack_trailer():
    trailer = ik.pack_trailer(12345, ik.TYPE_VALUE)
    seq, vtype = ik.unpack_trailer(trailer)
    assert seq == 12345
    assert vtype == ik.TYPE_VALUE


def test_make_and_parse_internal_key():
    key = ik.make_internal_key(b"alpha", 7, ik.TYPE_VALUE)
    user, seq, vtype = ik.parse_internal_key(key)
    assert user == b"alpha"
    assert seq == 7
    assert vtype == ik.TYPE_VALUE
    assert ik.extract_user_key(key) == b"alpha"


def test_trailer_out_of_range():
    with pytest.raises(ValueError):
        ik.pack_trailer(-1, ik.TYPE_VALUE)
    with pytest.raises(ValueError):
        ik.pack_trailer(ik.MAX_SEQUENCE + 1, ik.TYPE_VALUE)


def test_user_key_ordering_dominates():
    a = ik.make_internal_key(b"a", 100, ik.TYPE_VALUE)
    b = ik.make_internal_key(b"b", 1, ik.TYPE_VALUE)
    assert ik.compare_internal_keys(a, b) < 0


def test_newer_sequence_sorts_first_for_same_user_key():
    older = ik.make_internal_key(b"k", 5, ik.TYPE_VALUE)
    newer = ik.make_internal_key(b"k", 9, ik.TYPE_VALUE)
    # Larger sequence (newer) must compare as "less" so it sorts first.
    assert ik.compare_internal_keys(newer, older) < 0
    assert ik.compare_internal_keys(older, newer) > 0
    assert ik.compare_internal_keys(older, older) == 0


def test_lookup_key_lands_on_newest_visible_version():
    # Simulate a sorted run of versions for one user key.
    entries = [
        ik.make_internal_key(b"k", 10, ik.TYPE_VALUE),
        ik.make_internal_key(b"k", 7, ik.TYPE_VALUE),
        ik.make_internal_key(b"k", 3, ik.TYPE_VALUE),
    ]
    entries.sort(key=cmp_to_key(ik.compare_internal_keys))
    # Order should be newest-first.
    seqs = [ik.parse_internal_key(e)[1] for e in entries]
    assert seqs == [10, 7, 3]

    # A lookup as of sequence 8 should find the version with seq 7 (the newest
    # version not newer than 8): it is the first entry >= the lookup key.
    lookup = ik.make_lookup_key(b"k", 8)
    first_ge = next(e for e in entries if ik.compare_internal_keys(e, lookup) >= 0)
    assert ik.parse_internal_key(first_ge)[1] == 7


def test_deletion_type_distinct_from_value():
    val = ik.make_internal_key(b"k", 5, ik.TYPE_VALUE)
    dele = ik.make_internal_key(b"k", 5, ik.TYPE_DELETION)
    assert val != dele
    # At equal sequence, the larger type (value=1) sorts before deletion (0).
    assert ik.compare_internal_keys(val, dele) < 0


def test_full_sort_matches_user_then_version():
    raw = [
        (b"b", 1),
        (b"a", 2),
        (b"a", 5),
        (b"a", 3),
        (b"c", 9),
        (b"b", 4),
    ]
    keys = [ik.make_internal_key(u, s, ik.TYPE_VALUE) for u, s in raw]
    keys.sort(key=cmp_to_key(ik.compare_internal_keys))
    parsed = [(ik.parse_internal_key(k)[0], ik.parse_internal_key(k)[1]) for k in keys]
    assert parsed == [
        (b"a", 5),
        (b"a", 3),
        (b"a", 2),
        (b"b", 4),
        (b"b", 1),
        (b"c", 9),
    ]
