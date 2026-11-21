"""Exercise batched scene cache reads in the real store publication scope."""

import pytest

from trpg_state_machine.runtime_instances import resolve_field_batch
from trpg_state_machine.storage import SqliteStore


def resolve(store, cells, compute):
    return resolve_field_batch(
        store,
        "scene",
        "session.local",
        "scene.room",
        "lighting",
        "cells",
        cells,
        compute,
        capacity=2,
        maintained="light_result",
    )


def never_compute(misses):
    pytest.fail(f"Expected only cache hits, got {misses!r}")


def test_area_keeps_hits_before_misses_can_evict_them(tmp_path):
    store = SqliteStore(tmp_path / "fields.db")
    try:
        assert resolve(store, ("cached",), lambda _: {"cached": ["old"]}) == {"cached": ["old"]}

        def fill(misses):
            assert misses == ("first", "last")
            return {cell: [cell] for cell in misses}

        results = resolve(store, ("first", "cached", "last", "first"), fill)
        assert results == {"first": ["first"], "cached": ["old"], "last": ["last"]}
        results["last"].append("caller mutation")
        assert resolve(store, ("first", "last"), never_compute) == {
            "first": ["first"],
            "last": ["last"],
        }
    finally:
        store.close()


def test_rolled_back_area_results_do_not_replace_current_cache(tmp_path):
    store = SqliteStore(tmp_path / "fields.db")
    try:
        old = resolve(store, ("target",), lambda _: {"target": ["before"]})
        with store.transaction(write=True):
            with pytest.raises(RuntimeError, match="undo candidate"), store.savepoint():
                store.memory._advance_field("light_sources", "session.local", "scene.room")
                values = resolve(store, ("target",), lambda _: {"target": ["candidate"]})
                assert values == {"target": ["candidate"]}
                raise RuntimeError("undo candidate")
            assert resolve(store, ("target",), never_compute) == old

        assert resolve(store, ("target",), never_compute) == {"target": ["before"]}
    finally:
        store.close()
