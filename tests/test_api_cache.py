"""Tests for the CCClient in-memory cache bounds (size + TTL sweep)."""
from __future__ import annotations

import time

from collectorcrypt.api import CCClient


def _client(*, ttl=30.0, maxsize=500):
    # No network is touched: we exercise the cache helpers directly.
    return CCClient(cache_ttl=ttl, cache_max_entries=maxsize)


def test_cache_roundtrip():
    c = _client()
    c._cache_set(("k",), {"v": 1})
    assert c._cache_get(("k",)) == {"v": 1}


def test_cache_expiry_returns_none():
    c = _client(ttl=0.01)
    c._cache_set(("k",), 1)
    time.sleep(0.02)
    assert c._cache_get(("k",)) is None


def test_cache_evicts_expired_on_set():
    c = _client(ttl=0.01)
    c._cache_set(("old",), 1)
    time.sleep(0.02)
    # Writing a fresh entry sweeps the now-expired one out of the dict.
    c._cache_set(("new",), 2)
    assert ("old",) not in c._cache
    assert ("new",) in c._cache


def test_cache_bounded_to_max_entries():
    c = _client(maxsize=3)
    for i in range(10):
        c._cache_set((i,), i)
    assert len(c._cache) == 3
    # Oldest (least-recently-used) keys were evicted; newest survive.
    assert (9,) in c._cache
    assert (0,) not in c._cache


def test_cache_get_refreshes_lru_recency():
    c = _client(maxsize=3)
    c._cache_set(("a",), 1)
    c._cache_set(("b",), 2)
    c._cache_set(("c",), 3)
    # Touch "a" so it is most-recently-used, then overflow by one.
    assert c._cache_get(("a",)) == 1
    c._cache_set(("d",), 4)
    # "b" was the least-recently-used and should have been evicted, not "a".
    assert ("a",) in c._cache
    assert ("b",) not in c._cache
