"""Production-path costs: dependency probes, copies and association discovery."""

from collections import Counter
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace

import pytest
from lock_support import CRED, A
from test_mechanism_service import extended_data, trigger
from test_scoped_runtime_validation import grow, measure
from test_world_containers import BAG, BOX, container_data
from test_world_lights import lighting_data
from test_world_mechanisms import LEVER
from world_support import command, setup, submit

from trpg_state_machine import (
    PickupItem,
    PutItem,
    SetLightEnabled,
    StepActor,
    TakeItem,
    runtime_instances,
)


def many_contents(n):
    data = container_data()
    obj = next(o for o in data["scenes"][0]["objects"] if o["object_id"] == CRED)
    item = next(i for i in data["item_transfers"] if i["instance_id"] == CRED)
    profile = next(d for d in data["world_objects"] if d["object_id"] == CRED)
    identities = tuple(f"object.unit_{i}" for i in range(n))
    for oid in identities:
        data["scenes"][0]["objects"].append({**deepcopy(obj), "object_id": oid})
        data["item_transfers"].append({**deepcopy(item), "instance_id": oid})
        data["world_objects"].append(
            {**deepcopy(profile), "object_id": oid, "definition_ref": "definition." + oid[7:]}
        )
    for d in data["world_objects"]:
        for c in d["capabilities"]:
            if c["kind"] == "container":
                c.update(capacity_weight_lb=1000, capacity_volume_cu_ft=1000)
    return data, identities


@contextmanager
def work(sm, monkeypatch):
    counts = Counter()
    memory = sm._store.memory
    before = dict(memory.stats)
    with monkeypatch.context() as patch, measure(sm) as (sql, _):
        for obj, name, label in (
            (memory, "version", "dependency_checks"),
            (memory, "select", "ram_queries"),
            (sm._world_facts, "_discover_capability_ids", "association_discoveries"),
        ):
            original = getattr(obj, name)

            def counted(*args, _original=original, _label=label, **kwargs):
                counts[_label] += 1
                result = _original(*args, **kwargs)
                if _label == "dependency_checks":
                    for domain in ("weight", "mechanism_index", "light_candidates"):
                        if args[0][0].startswith(domain + ":"):
                            counts[domain + "_checks"] += 1
                return result

            patch.setattr(obj, name, counted)
        original_copy = runtime_instances.deepcopy

        def copied(value):
            if (
                isinstance(value, dict)
                and value
                and all(isinstance(k, tuple) and len(k) == 3 for k in value)
            ):
                counts["copied_trigger_records"] += sum(len(v) for v in value.values())
            return original_copy(value)

        patch.setattr(runtime_instances, "deepcopy", copied)
        from trpg_state_machine.mechanism_models import MechanismTrigger

        trigger_repr = MechanismTrigger.__repr__

        def represented(trigger):
            counts["represented_trigger_records"] += 1
            return trigger_repr(trigger)

        patch.setattr(MechanismTrigger, "__repr__", represented)
        scene_index = sm._objects.mechanisms._scene_index

        class ObservedIndex:
            def __init__(self, index):
                self.index = index

            def get(self, key, default=()):
                selected = self.index.get(key, default)
                counts["index_candidate_records"] += len(selected)
                return selected

        patch.setattr(
            sm._objects.mechanisms,
            "_scene_index",
            lambda *args, **kwargs: ObservedIndex(scene_index(*args, **kwargs)),
        )
        original_resolve = runtime_instances.resolve_field

        def resolve(*args, **kwargs):
            # Outside scopes the original opens a scope and re-enters here.
            if args[0]._scope is None:
                return original_resolve(*args, **kwargs)
            compute = args[7]

            def derived():
                counts[f"compute:{args[4]}.{args[5]}"] += 1
                return compute()

            return original_resolve(*args[:7], derived, **kwargs)

        patch.setattr(runtime_instances, "resolve_field", resolve)
        yield counts
        counts.update(sql)
        for key in before:
            if not key.endswith("_ns"):
                counts[key] = memory.stats[key] - before[key]


@pytest.mark.parametrize("n", (10, 100))
def test_warm_weight_probes(tmp_path, context, monkeypatch, n):
    data, identities = many_contents(n)
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        with sm._transaction(write=True):
            for oid in identities:
                sm._world_facts.transfer_item(
                    sid, sm._world_facts.object(sid, oid), container_id=BOX
                )
        svc = sm._objects.containers
        assert svc.contents_weight(sid, BOX) == n
        with work(sm, monkeypatch) as counts:
            assert svc.contents_weight(sid, BOX) == n
        print("WEIGHT", n, dict(counts))
        assert counts["instance_builds"] == 0
        assert counts["dependency_checks"] == counts["weight_checks"] == 1
        assert counts["sql_selects"] == counts["sql_rows"] == counts["ram_queries"] == 0
        with work(sm, monkeypatch) as repeated:
            for _ in range(3):
                assert svc.contents_weight(sid, BOX) == n
        assert repeated["dependency_checks"] == 3 and repeated["instance_builds"] == 0
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        sm.get_object(sid, actor, BOX, package, context=context)
        sm.list_scene_objects(sid, actor, package, context=context)
        with work(sm, monkeypatch) as public:
            assert (
                sm.get_object(sid, actor, BOX, package, context=context)["contents_weight_lb"] == n
            )
            assert (
                next(
                    v
                    for v in sm.list_scene_objects(sid, actor, package, context=context)
                    if v["object_id"] == BOX
                )["contents_weight_lb"]
                == n
            )
        assert public["weight_checks"] <= 6  # Full contents output is deliberately not O(1).
        assert public["compute:container.contents_weight"] == 0


@pytest.mark.parametrize("n", (10, 100))
@pytest.mark.parametrize("family", ("object_operation", "enter_region"))
def test_nonmatching_mechanism_records(tmp_path, context, monkeypatch, n, family):
    data = extended_data(
        triggers=[
            trigger(
                "object_operation",
                target_object_id=CRED,
                event_type="object.light_changed",
                value=True,
            )
        ]
    )
    if family == "enter_region":
        next(d for d in data["world_objects"] if d["object_id"] == LEVER)["capabilities"][0][
            "triggers"
        ] = [trigger("enter_region", cells=["2,0"])]
    obj = next(o for o in data["scenes"][0]["objects"] if o["object_id"] == LEVER)
    definition = next(o for o in data["world_objects"] if o["object_id"] == LEVER)
    for i in range(1, n):
        oid = f"object.mech_{i}"
        data["scenes"][0]["objects"].append({**deepcopy(obj), "object_id": oid})
        data["world_objects"].append(
            {**deepcopy(definition), "object_id": oid, "definition_ref": f"definition.mech_{i}"}
        )
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        sm._objects.mechanisms._scene_index(sid, A)
        motion = command(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "motion")
        with work(sm, monkeypatch) as counts:
            receipt = sm.submit_command(motion, package, context=context)
        assert receipt.status == "committed", receipt
        print("MECHANISM_REGION" if family == "enter_region" else "MECHANISM", n, dict(counts))
        assert counts["copied_trigger_records"] == counts["compute:mechanisms.index"] == 0
        assert counts["represented_trigger_records"] == 0
        assert counts["index_candidate_records"] == 0
        assert counts["mechanism_index_checks"] == 1
        # Scene record/version lookup now replaces content-key map lookup.
        assert counts["dependency_checks"] <= 114 and counts["ram_rows"] <= 32
        key = ("instance", sid, "scene", A)
        entry = sm._store.memory.values[key][0].mechanisms.index[0]
        assert (
            sm._store.memory.sizes[key] >= entry.retained_bytes >= len(repr(entry.value).encode())
        )


@pytest.mark.parametrize("growth", (0, 100))
def test_empty_bag_does_not_rediscover_scene(tmp_path, context, monkeypatch, growth):
    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        grow(sm, sid, growth)
        assert (
            submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on").status
            == "committed"
        )
        receipt = submit(sm, sid, package, context, PickupItem(BAG, A, "1,0"), "bag")
        assert receipt.status == "committed", receipt
        # Warm both associations and real event/light consumers.
        for i in range(2):
            assert (
                submit(
                    sm, sid, package, context, PutItem(A, BAG, BOX, "0,0"), f"warm-put{i}"
                ).status
                == "committed"
            )
            assert (
                submit(
                    sm, sid, package, context, TakeItem(A, BAG, BOX, "0,0"), f"warm-take{i}"
                ).status
                == "committed"
            )
        # A full SessionView is a separate full-output request. Reuse its version
        # and then receipt versions, as production command consumers can do.
        put = command(sm, sid, package, context, PutItem(A, BAG, BOX, "0,0"), "put")
        take = command(sm, sid, package, context, TakeItem(A, BAG, BOX, "0,0"), "take")
        version = put.expected_world_version
        with work(sm, monkeypatch) as counts:
            for i in range(2):
                for template in (put, take):
                    op = replace(
                        template,
                        command_id=f"{template.command_id}.{i}",
                        expected_world_version=version,
                    )
                    receipt = sm.submit_command(op, package, context=context)
                    assert receipt.status == "committed", receipt
                    version = receipt.world_version
        print("EMPTY_BAG", growth, dict(counts))
        assert counts["association_discoveries"] == counts["capability_nodes"] == 0
        assert counts["compute:mechanisms.index"] == counts["compute:lighting.sources"] == 0
        assert counts["compute:lighting.candidates"] == counts["compute:lighting.geometry"] == 0
        assert counts["ram_queries"] <= 284 and counts["ram_rows"] <= 180
        assert counts["dependency_checks"] <= 1000


def change_weight(sm, sid, oid, weight):
    import json

    from trpg_state_machine.instance_data import encode
    from trpg_state_machine.package import canonical_json

    data = sm._world_facts.instances.get(sid, "object", oid)
    definition = json.loads(data["definition"])
    definition["fields"]["weight_lb"] = weight
    data["definition"] = canonical_json(definition)
    sm._conn.execute(
        "UPDATE instance_configs SET config_json=? "
        "WHERE session_id=? AND domain='object' AND instance_id=?",
        (canonical_json(encode(data)), sid, oid),
    )


def test_weight_mutations_use_candidate_version_and_rollback(tmp_path, context):
    import json

    from test_lockable_passages import acquire

    from trpg_state_machine import StateMachine
    from trpg_state_machine.package import canonical_json

    sm, sid, package, db = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        assert (
            submit(sm, sid, package, context, PutItem(A, BAG, BOX, "0,0"), "bag").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, PutItem(A, CRED, BAG, "0,0"), "key").status
            == "committed"
        )
        svc = sm._objects.containers
        assert svc.contents_weight(sid, BOX) == 3
        assert svc.current_weight(sid, BOX) == 5
        with sm._transaction(write=True):
            # The boundary observes quantity/own mass even though public partial
            # stack transfer remains unsupported. Restore the unit before commit.
            item = sm._world_facts.object(sid, CRED)
            data = json.loads(item["item_json"])
            data["quantity"] = 2
            sm._conn.execute(
                "UPDATE objects SET item_json=? WHERE session_id=? AND object_id=?",
                (canonical_json(data), sid, CRED),
            )
            assert svc.own_weight(sid, CRED) == 2
            assert svc.contents_weight(sid, BOX) == 4
            change_weight(sm, sid, CRED, 3)
            assert svc.contents_weight(sid, BOX) == 8
            # A computed candidate value cannot survive rollback with a new stamp.
            with pytest.raises(ValueError), sm._store.savepoint():
                change_weight(sm, sid, CRED, None)
                assert svc.contents_weight(sid, BOX) is None
                raise ValueError("savepoint")
            assert svc.contents_weight(sid, BOX) == 8
            sm._conn.execute(
                "UPDATE objects SET item_json=? WHERE session_id=? AND object_id=?",
                (item["item_json"], sid, CRED),
            )
            assert svc.contents_weight(sid, BOX) == 5
            sm.scene_lifecycle._validate_scene(sid, A, package)
        with pytest.raises(ValueError), sm._transaction(write=True):
            change_weight(sm, sid, CRED, None)
            assert svc.current_weight(sid, BOX) is None
            raise ValueError("outer rollback")
        assert svc.current_weight(sid, BOX) == 7
        # Candidate capacity checks use the relocated state, not the warm total.
        candidate = svc._rows(sid, roots=(BOX, BAG, CRED))
        candidate.relocate(CRED, container_id=BOX)
        assert svc.total_weight(BAG, candidate, package) == 2
        assert svc.total_weight(BOX, candidate, package) == 7
        assert svc.contents_weight(sid, BAG) == 3
        sm.unload_session(sid, package, context=context)
        assert svc.contents_weight(sid, BOX) == 5
    with StateMachine(db, dnd_loader=sm._rules.loader) as restored:
        restored.resume_session(sid, package, context=context)
        assert restored._objects.containers.contents_weight(sid, BOX) == 5


def test_capability_subtree_shading_carrier_move_and_rollback(tmp_path, context, monkeypatch):
    from dataclasses import FrozenInstanceError

    from lock_support import B
    from test_mechanism_service import config

    from trpg_state_machine import EnterScene, ObjectState, SetObjectState, StateMachine

    data = lighting_data()
    data["manifest"]["schema_version"] = "state-machine-foundation/10"
    next(d for d in data["world_objects"] if d["object_id"] == BAG)["capabilities"].append(
        config(triggers=[trigger("enter_region", cells=["2,0"])])
    )
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert (
            submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, PickupItem(BAG, A, "1,0"), "bag").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, PickupItem(BOX, A, "1,0"), "box").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, PutItem(A, CRED, BAG, "0,0"), "nested").status
            == "committed"
        )
        facts, mech = sm._world_facts, sm._objects.mechanisms
        old_snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        with sm._transaction(write=True):
            sm.scene_lifecycle.ensure_scene(sid, B, package)
        old_members = facts.scene_capability_ids(sid, A, "object_mechanisms")
        assert facts.scene_capability_ids(sid, A, table="object_mechanisms") == old_members
        old_index = mech._scene_index(sid, A)
        facts.scene_capability_ids(sid, B, "object_lights")
        facts.scene_capability_ids(sid, B, "object_mechanisms")
        mech._scene_index(sid, B)
        key = ("enter_region", A, "2,0")
        with pytest.raises(TypeError):
            old_index[key] = ()
        with pytest.raises(FrozenInstanceError):
            old_index[key][0][2].scene_id = B
        with work(sm, monkeypatch) as same_scene:
            assert (
                submit(sm, sid, package, context, PutItem(A, BAG, BOX, "0,0"), "in").status
                == "committed"
            )
            assert (
                submit(
                    sm, sid, package, context, SetObjectState(A, BOX, ObjectState.CLOSED), "shade"
                ).status
                == "committed"
            )
            assert (
                sm.get_illumination(sid, actor, "1,0", package, context=context)["illumination"]
                == "dark"
            )
            assert (
                submit(
                    sm, sid, package, context, SetObjectState(A, BOX, ObjectState.OPEN), "unshade"
                ).status
                == "committed"
            )
            assert (
                sm.get_illumination(sid, actor, "1,0", package, context=context)["illumination"]
                == "bright"
            )
            assert (
                submit(sm, sid, package, context, TakeItem(A, BAG, BOX, "0,0"), "out").status
                == "committed"
            )
        assert same_scene["association_discoveries"] == same_scene["compute:mechanisms.index"] == 0
        with work(sm, monkeypatch) as moved:
            assert submit(sm, sid, package, context, EnterScene(B), "cross").status == "committed"
            assert facts.scene_capability_ids(sid, A, "object_lights") == ()
            assert facts.scene_capability_ids(sid, A, "object_mechanisms") == ()
            assert facts.scene_capability_ids(sid, B, "object_lights") == (CRED,)
            assert facts.scene_capability_ids(sid, B, "object_mechanisms") == (BAG,)
            assert facts.scene_capability_ids(sid, B, table="object_mechanisms") == (BAG,)
        assert moved["association_discoveries"] == 0
        assert old_members == (BAG,) and old_index[key][0][0] == BAG
        assert old_snapshot.scene_state.scene_id == A
        for savepoint in (True, False):
            with sm._transaction(write=True) if savepoint else nullcontext():
                boundary = sm._store.savepoint() if savepoint else sm._transaction(write=True)
                with pytest.raises(ValueError), boundary:
                    facts.transfer_item(sid, facts.object(sid, BAG), scene_id=A, cell="1,0")
                    assert facts.scene_capability_ids(sid, A, "object_lights") == (CRED,)
                    assert facts.scene_capability_ids(sid, B, "object_lights") == ()
                    mech._scene_index(sid, A)
                    raise ValueError("rollback")
            assert facts.scene_capability_ids(sid, A, "object_lights") == ()
            assert facts.scene_capability_ids(sid, B, "object_mechanisms") == (BAG,)
        # Capability membership edits patch only their actual scene; restore the
        # row before structural acceptance (there is no public capability editor).
        with sm._transaction(write=True), work(sm, monkeypatch) as edited:
            for table, oid in (("object_lights", CRED), ("object_mechanisms", BAG)):
                state = sm._conn.execute(
                    f"SELECT * FROM {table} WHERE session_id=? AND object_id=?", (sid, oid)
                ).fetchone()
                sm._conn.execute(
                    f"DELETE FROM {table} WHERE session_id=? AND object_id=?", (sid, oid)
                )
                assert facts.scene_capability_ids(sid, B, table) == ()
                placeholders = ",".join("?" for _ in state)
                sm._conn.execute(f"INSERT INTO {table} VALUES ({placeholders})", tuple(state))
                assert facts.scene_capability_ids(sid, B, table) == (oid,)
            sm.scene_lifecycle._validate_scene(sid, A, package)
            sm.scene_lifecycle._validate_scene(sid, B, package)
        assert edited["association_discoveries"] == 0
        sm.unload_session(sid, package, context=context)
        assert facts.scene_capability_ids(sid, B, "object_lights") == (CRED,)
    with StateMachine(db, dnd_loader=sm._rules.loader) as restored:
        restored.resume_session(sid, package, context=context)
        assert restored._world_facts.scene_capability_ids(sid, B, "object_mechanisms") == (BAG,)
        assert restored._world_facts.scene_capability_ids(sid, A, "object_lights") == ()
