"""A probabilistic skip list — the memtable's ordered index.

A skip list keeps entries sorted while supporting O(log n) insert and search
without the rebalancing bookkeeping of a tree, which makes it a natural fit for
a write-heavy in-memory structure. Each node carries a stack of forward
pointers; higher levels skip exponentially further ahead, so a search descends
levels like a balanced-tree traversal.

This implementation is single-writer (guarded by the database's write lock) but
supports any number of concurrent readers walking level 0, which is all the
memtable needs.
"""

from __future__ import annotations

import random
from collections.abc import Callable

MAX_HEIGHT = 12
# p = 1/BRANCHING chance of promoting a node to the next level.
BRANCHING = 4


class _Node:
    __slots__ = ("key", "value", "forward")

    def __init__(self, key, value, height: int):
        self.key = key
        self.value = value
        # forward[i] is the next node at level i.
        self.forward: list[_Node | None] = [None] * height


class SkipList:
    """An ordered map keyed by ``bytes`` under a three-way ``comparator``."""

    def __init__(self, comparator: Callable[[bytes, bytes], int]):
        self._cmp = comparator
        self._head = _Node(None, None, MAX_HEIGHT)
        self._height = 1
        self._rng = random.Random()
        self._count = 0

    def __len__(self) -> int:
        return self._count

    def _random_height(self) -> int:
        height = 1
        while height < MAX_HEIGHT and self._rng.randrange(BRANCHING) == 0:
            height += 1
        return height

    def _find_greater_or_equal(self, key, update: list | None):
        """Return the first node with ``node.key >= key``.

        If ``update`` (a list of length ``MAX_HEIGHT``) is supplied, it is
        filled with the last node seen at each level that precedes ``key`` —
        the predecessors an insert needs to splice between.
        """
        node = self._head
        for level in range(self._height - 1, -1, -1):
            nxt = node.forward[level]
            while nxt is not None and self._cmp(nxt.key, key) < 0:
                node = nxt
                nxt = node.forward[level]
            if update is not None:
                update[level] = node
        return node.forward[0]

    def insert(self, key, value) -> None:
        """Insert ``key`` (or overwrite its value if already present)."""
        update: list[_Node | None] = [self._head] * MAX_HEIGHT
        candidate = self._find_greater_or_equal(key, update)
        if candidate is not None and self._cmp(candidate.key, key) == 0:
            candidate.value = value
            return

        height = self._random_height()
        if height > self._height:
            for level in range(self._height, height):
                update[level] = self._head
            self._height = height

        node = _Node(key, value, height)
        for level in range(height):
            node.forward[level] = update[level].forward[level]
            update[level].forward[level] = node
        self._count += 1

    def get(self, key):
        """Return the value for an exact key match, or ``None`` if absent."""
        node = self._find_greater_or_equal(key, None)
        if node is not None and self._cmp(node.key, key) == 0:
            return node.value
        return None

    def contains(self, key) -> bool:
        node = self._find_greater_or_equal(key, None)
        return node is not None and self._cmp(node.key, key) == 0

    def __iter__(self):
        node = self._head.forward[0]
        while node is not None:
            yield node.key, node.value
            node = node.forward[0]

    def iterator(self) -> SkipListIterator:
        return SkipListIterator(self)

    # Internal hook used by the iterator.
    def _first(self):
        return self._head.forward[0]

    def _seek(self, key):
        return self._find_greater_or_equal(key, None)


class SkipListIterator:
    """A seekable forward cursor over a :class:`SkipList`.

    Implements the common iterator protocol used throughout the engine
    (``valid`` / ``key`` / ``value`` / ``seek`` / ``seek_to_first`` / ``next``)
    so it can feed the merging iterator alongside on-disk table iterators.
    """

    def __init__(self, skiplist: SkipList):
        self._list = skiplist
        self._node: _Node | None = None

    def valid(self) -> bool:
        return self._node is not None

    def key(self):
        assert self._node is not None, "key() on an invalid iterator"
        return self._node.key

    def value(self):
        assert self._node is not None, "value() on an invalid iterator"
        return self._node.value

    def seek_to_first(self) -> None:
        self._node = self._list._first()

    def seek(self, target) -> None:
        self._node = self._list._seek(target)

    def next(self) -> None:
        assert self._node is not None, "next() on an invalid iterator"
        self._node = self._node.forward[0]


__all__ = ["SkipList", "SkipListIterator", "MAX_HEIGHT", "BRANCHING"]
