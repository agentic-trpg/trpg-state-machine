"""World-owned passage associations through real commands and candidate scopes."""

from collections import Counter
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace

import pytest
from lock_support import CRED, DOOR, OTHER_DOOR, A, B, C, lock_loader
from test_scoped_runtime_validation import grow_scenes, measure
from test_world_containers import BAG, BOX
from test_world_lights import lighting_data
from world_support import setup, submit

from trpg_state_machine import (
    ObjectState,
    SetLightEnabled,
    SetObjectState,
    StateMachine,
    StateMachineError,
    instance_data,
)
from trpg_state_machine.instance_data import encode, passage_index
from trpg_state_machine.package import canonical_json
from trpg_state_machine.runtime_instances import PassageIndex


def passage_data():
    data = lighting_data()
    # Public object authorization must also work after the door blocks its own
    # threshold. Ambient bright keeps this performance fixture independent of LOS.
    data["scenes"][0]["grid"]["default_lighting"] = "bright"
    data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"][
        "default_lighting"
    ] = "bright"
    return data


@contextmanager
def passage_work(sm, monkeypatch):
    """Count actual helper SQLite opcodes, not just returned rows/cache builds."""
    memory = sm._store.memory
    helper, version = memory._light_door_scenes, memory.version
    counts = Counter()
    native = sm._store._conn
    build, decode, loads = PassageIndex.build, instance_data.decode, instance_data.json.loads
    select = memory.select
    helper_depth = 0

    def step():
        counts["sql_vm_steps"] += 1
        if helper_depth:
            counts["door_sql_vm_steps"] += 1
        return 0

    class Passages(tuple):
        def __iter__(self):
            for passage in super().__iter__():
                counts["passage_visits"] += 1
                yield passage

    def prepare(cls, passages):
        counts["index_builds"] += 1
        return build(Passages(passages))

    def parse(value, *args, **kwargs):
        if isinstance(value, str) and '"PassageDefinition"' in value:
            counts["passage_json_parses"] += 1
        return loads(value, *args, **kwargs)

    def record(value):
        if isinstance(value, dict) and value.get("type") == "PassageDefinition":
            counts["passage_decodes"] += 1
        return decode(value)

    def ram(*args):
        counts["ram_queries"] += 1
        rows = select(*args)
        source = rows.rows

        def consumed():
            for row in source:
                counts["ram_rows"] += 1
                yield row

        rows.rows = consumed()
        return rows

    def lookup(*args):
        nonlocal helper_depth
        counts["door_lookups"] += 1
        helper_depth += 1
        try:
            return helper(*args)
        finally:
            helper_depth -= 1

    def check(dep):
        counts["version_checks"] += 1
        if dep[0].startswith("passage_index:"):
            counts["passage_version_checks"] += 1
        return version(dep)

    with monkeypatch.context() as patch, measure(sm) as (sql, _):
        patch.setattr(memory, "_light_door_scenes", lookup)
        patch.setattr(memory, "version", check)
        patch.setattr(memory, "select", ram)
        patch.setattr(PassageIndex, "build", classmethod(prepare))
        patch.setattr(instance_data, "decode", record)
        patch.setattr(instance_data.json, "loads", parse)
        native.set_progress_handler(step, 1)
        try:
            yield counts
        finally:
            native.set_progress_handler(None, 0)
            counts.update(sql)


@pytest.mark.parametrize("extra", (0, 10, 100))
@pytest.mark.parametrize("objects", ((BOX, BAG), (DOOR,)))
def test_public_object_changes_use_shared_passage_index(
    tmp_path, context, monkeypatch, extra, objects
):
    sm, sid, package, _ = setup(tmp_path, context, passage_data())
    with sm:
        with sm._transaction(write=True):
            sm.scene_lifecycle.ensure_scene(sid, B, package)
        grow_scenes(sm, sid, extra * 2)
        assert (
            submit(
                sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "light.on"
            ).status
            == "committed"
        )
        # Prepare the shared World layout through the existing production entry.
        sm._world_facts.instances.passages(sid, scene_id=A)
        for state in (ObjectState.CLOSED, ObjectState.OPEN):
            for oid in objects:
                with passage_work(sm, monkeypatch) as counts:
                    receipt = submit(
                        sm, sid, package, context, SetObjectState(A, oid, state), f"{oid}.{state}"
                    )
                    assert receipt.status == "committed", receipt
                print(extra, oid, state, dict(counts))
                assert counts["door_sql_vm_steps"] == 0
                assert counts["door_lookups"] == 1
                assert counts["index_builds"] == counts["passage_visits"] == 0
                assert counts["passage_json_parses"] == counts["passage_decodes"] == 0
                assert counts["passage_version_checks"] < 15


@pytest.mark.parametrize("extra", (0, 10, 100))
def test_cold_preparation_is_shared_and_only_hot_scalar_version_is_checked(
    tmp_path, context, monkeypatch, extra
):
    sm, sid, package, _ = setup(tmp_path, context, passage_data())
    with sm:
        with sm._transaction(write=True):
            sm.scene_lifecycle.ensure_scene(sid, B, package)
        grow_scenes(sm, sid, extra * 2)
        sm.unload_session(sid, package, context=context)
        with passage_work(sm, monkeypatch) as cold:
            assert sm._world_facts.instances.passages(sid, object_id=BOX) == ()
        assert cold["index_builds"] == cold["passage_json_parses"] == 1
        assert cold["passage_decodes"] == cold["passage_visits"] == extra + 2
        assert cold["sql_selects"] == cold["sql_rows"] == 1
        print("cold", extra, dict(cold))
        with passage_work(sm, monkeypatch) as hot:
            assert sm._world_facts.instances.passages(sid, object_id=BAG) == ()
            assert len(sm._world_facts.instances.passages(sid, object_id=DOOR)) == 1
            assert sm._store.memory._light_door_scenes(sid, DOOR) == (A, B)
        assert hot["sql_selects"] == hot["passage_visits"] == hot["index_builds"] == 0
        assert hot["passage_decodes"] == hot["passage_json_parses"] == 0
        assert hot["passage_version_checks"] == 3
        print("hot buckets", extra, dict(hot))


def write_layout(sm, sid, passages, *, replace_row=False):
    config = sm._world_facts.instances.get(sid, "session", sid)
    config["passages"] = passages
    if replace_row:
        sm._conn.execute(
            "INSERT OR REPLACE INTO instance_configs VALUES (?,?,?,?)",
            (sid, "session", sid, canonical_json(encode(config))),
        )
    else:
        sm._conn.execute(
            "UPDATE instance_configs SET config_json=? WHERE session_id=? "
            "AND domain='session' AND instance_id=?",
            (canonical_json(encode(config)), sid, sid),
        )


def test_shared_door_multiple_passages_and_scene_light_maintenance(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, passage_data())
    with sm:
        with sm._transaction(write=True):
            for scene in (B, C):
                sm.scene_lifecycle.ensure_scene(sid, scene, package)
        old = passage_index(sm._store, sid)
        assert old.door_scenes[DOOR] == (A, B)
        shared = tuple(replace(p, door_object_id=DOOR) for p in old.passages)
        with sm._transaction(write=True):
            write_layout(sm, sid, shared)
        current = passage_index(sm._store, sid)
        assert current.door_scenes[DOOR] == (A, B, C)
        assert current.select(object_id=OTHER_DOOR) == ()
        for scene in (A, B, C):
            expected = tuple(p for p in shared if any(e.scene_id == scene for e in p.endpoints))
            assert current.select(scene_id=scene, object_id=DOOR) == expected
            assert sm._world_facts.instances.passages(sid, scene_id=scene) == expected
        assert deepcopy(current) is current
        with pytest.raises(TypeError):
            current.by_door[DOOR] = ()
        assert (
            submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on").status
            == "committed"
        )
        lights = sm._objects.lights
        before = {s: lights.geometry(sid, s, package) for s in (A, B, C)}
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        cell = next(e.threshold_cell for e in shared[0].endpoints if e.scene_id == A)
        old_light = sm.get_illumination(sid, actor, cell, package, context=context)
        assert (
            submit(
                sm, sid, package, context, SetObjectState(A, DOOR, ObjectState.CLOSED), "close"
            ).status
            == "committed"
        )
        for scene in (A, B, C):
            thresholds = {
                e.threshold_cell for p in shared for e in p.endpoints if e.scene_id == scene
            }
            assert thresholds <= lights.geometry(sid, scene, package).candidates
            assert not thresholds.intersection(before[scene].candidates)
        closed_light = sm.get_illumination(sid, actor, cell, package, context=context)
        assert old_light["status"] == "known" and closed_light["status"] == "unknown"
        assert (
            submit(
                sm, sid, package, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open"
            ).status
            == "committed"
        )
        for scene in (A, B, C):
            assert lights.geometry(sid, scene, package).candidates == before[scene].candidates
        assert sm.get_illumination(sid, actor, cell, package, context=context)["status"] == "known"
        assert old.door_scenes[DOOR] == (A, B) and old.by_door[OTHER_DOOR]


@pytest.mark.parametrize("boundary", ("savepoint", "transaction"))
def test_layout_changes_rollback_repeated_stamp_and_old_index(tmp_path, context, boundary):
    sm, sid, package, _ = setup(tmp_path, context, passage_data())
    with sm:
        old = passage_index(sm._store, sid)
        a, b = old.passages
        moved = replace(a, endpoints=(a.endpoints[0], replace(a.endpoints[1], scene_id=C)))
        candidate = None
        dep = (f"passage_index:{sid!r}", sid)
        with sm._transaction(write=True) if boundary == "savepoint" else nullcontext():
            manager = (
                sm._store.savepoint
                if boundary == "savepoint"
                else lambda: sm._transaction(write=True)
            )
            with pytest.raises(ValueError), manager():
                write_layout(sm, sid, (moved,))  # endpoint change and deletion
                candidate = passage_index(sm._store, sid)
                discarded_stamp = sm._store.memory.version(dep)
                assert candidate.door_scenes[DOOR] == (A, C)
                raise ValueError("rollback layout")
        assert passage_index(sm._store, sid) is old
        # Both alternatives advance the SAME numeric stamp from the restored root.
        # The discarded candidate must never be matched by that number alone.
        with sm._transaction(write=True):
            rebound = replace(a, door_object_id=BOX)
            added = replace(b, passage_id="passage.extra", door_object_id=BOX)
            write_layout(sm, sid, (rebound, b, added))
            accepted = passage_index(sm._store, sid)
            assert sm._store.memory.version(dep) == discarded_stamp
            assert DOOR not in accepted.door_scenes
            assert accepted.door_scenes[BOX] == (A, B, C)
            assert accepted.select(passage_id="passage.extra") == (added,)
        assert accepted is passage_index(sm._store, sid)
        assert old.door_scenes[DOOR] == (A, B)
        assert candidate.door_scenes[DOOR] == (A, C)


def test_replace_delete_recreate_unload_eviction_and_restart(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context, passage_data())
    with sm:
        old = passage_index(sm._store, sid)
        changed = (replace(old.passages[0], door_object_id=BOX),)
        with sm._transaction(write=True):
            write_layout(sm, sid, changed, replace_row=True)
        assert passage_index(sm._store, sid).door_scenes[BOX] == (A, B)
        with pytest.raises(ValueError), sm._transaction(write=True):
            sm._conn.execute(
                "DELETE FROM instance_configs WHERE session_id=? AND domain='session'", (sid,)
            )
            with pytest.raises(StateMachineError, match="Missing Session passages"):
                passage_index(sm._store, sid)
            raise ValueError("restore config")
        sm.unload_session(sid, package, context=context)
        assert passage_index(sm._store, sid).passages == changed
        sm._store.memory.forget(("instance", sid, "world", sid))
        assert passage_index(sm._store, sid).passages == changed
        assert old.door_scenes[DOOR] == (A, B)
    with StateMachine(db, dnd_loader=lock_loader()) as recovered:
        recovered.resume_session(sid, package, context=context)
        assert passage_index(recovered._store, sid).passages == changed
        assert recovered._store.memory._light_door_scenes(sid, DOOR) == ()


def test_empty_layout_and_unrelated_config_edits_keep_valid_negative_results(
    tmp_path, context, monkeypatch
):
    sm, sid, package, _ = setup(tmp_path, context, passage_data())
    with sm:
        with sm._transaction(write=True):
            write_layout(sm, sid, ())
        old = passage_index(sm._store, sid)
        assert old.passages == ()
        with sm._transaction(write=True):
            config = sm._world_facts.instances.get(sid, "session", sid)
            config["test_metadata"] = "not a layout change"
            sm._conn.execute(
                "UPDATE instance_configs SET config_json=? WHERE session_id=? "
                "AND domain='session' AND instance_id=?",
                (canonical_json(encode(config)), sid, sid),
            )
            config = sm._world_facts.instances.get(sid, "object", BOX)
            sm._conn.execute(
                "UPDATE instance_configs SET config_json=? WHERE session_id=? "
                "AND domain='object' AND instance_id=?",
                (canonical_json(encode({**config, "test_metadata": 1})), sid, BOX),
            )
        with passage_work(sm, monkeypatch) as hot:
            assert passage_index(sm._store, sid) is old
            assert sm._world_facts.instances.passages(sid, object_id=BOX) == ()
            assert sm._world_facts.instances.passages(sid, object_id=DOOR) == ()
        assert hot["index_builds"] == hot["sql_selects"] == hot["passage_json_parses"] == 0


@pytest.mark.parametrize("failure", ("publish", "unknown"))
def test_committed_layout_failure_requires_cross_connection_recovery(
    tmp_path, context, monkeypatch, failure
):
    first, sid, package, db = setup(tmp_path, context, passage_data())
    with first, StateMachine(db, dnd_loader=lock_loader()) as second:
        old_first = passage_index(first._store, sid)
        old_second = passage_index(second._store, sid)
        changed = (replace(old_first.passages[0], door_object_id=BOX),)
        commit = first._store._commit

        def fail_publish(scope):
            raise MemoryError("durable layout not published")

        def unknown():
            commit()
            raise OSError("lost commit acknowledgement")

        with monkeypatch.context() as patch:
            if failure == "publish":
                patch.setattr(first._store.memory, "publish", fail_publish)
            else:
                patch.setattr(first._store, "_commit", unknown)
            with pytest.raises(StateMachineError), first._transaction(write=True):
                write_layout(first, sid, changed)
                assert passage_index(first._store, sid).door_scenes[BOX] == (A, B)
        with pytest.raises(StateMachineError, match="reconciliation"):
            passage_index(first._store, sid)
        second.resume_session(sid, package, context=context)
        assert passage_index(first._store, sid).passages == changed
        assert passage_index(second._store, sid).passages == changed
        assert old_first.passages == old_second.passages == package.passages
