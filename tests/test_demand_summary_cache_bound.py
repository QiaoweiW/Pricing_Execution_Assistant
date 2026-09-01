"""Guard the ``_cached_fetch`` LRU bound against being set too tight.

Regression test for a real incident: ``_CACHE_MAX_ENTRIES`` was hard-coded to
8 — exactly the number of blobs the cache served — which left zero headroom.
The LRU then evicted *live* entries, and the 120 MB / 1.35 M-row plan-history
tracker was re-downloaded and re-parsed five times in forty minutes, well
inside its one-hour TTL.  On a memory-tight container that is both a large
transient allocation and tens of seconds of latency per render.

The bound must always leave room for every blob's CURRENT generation plus the
superseded ones it exists to evict.
"""
from __future__ import annotations

import data_sources.demand_summary as ds


def test_bound_leaves_room_for_every_blob_plus_superseded_generations():
    """Every blob must fit at least twice over — current + one superseded."""
    n_blobs = len(ds._CACHED_BLOB_PATHS)
    assert n_blobs > 0
    assert ds._CACHE_MAX_ENTRIES >= 2 * n_blobs, (
        f"max_entries={ds._CACHE_MAX_ENTRIES} is too tight for {n_blobs} "
        "blobs: the LRU will evict entries that are still current, forcing a "
        "full re-read of the 120 MB tracker on every render."
    )


def test_blob_list_covers_every_path_the_fetchers_use():
    """The derived bound is only correct if the list is complete.

    Any ``*_BLOB_PATH`` constant in the module is something a fetcher can hand
    to ``_cached_fetch``, so it has to be counted.  Catches the "added a ninth
    source, forgot the tuple" case that made the original bound wrong.
    """
    declared = {
        value for name, value in vars(ds).items()
        if name.endswith("_BLOB_PATH") and isinstance(value, str)
    }
    missing = declared - set(ds._CACHED_BLOB_PATHS)
    assert not missing, (
        f"blob path(s) not counted in _CACHED_BLOB_PATHS: {sorted(missing)} — "
        "the cache bound is derived from that tuple and is now too tight."
    )
