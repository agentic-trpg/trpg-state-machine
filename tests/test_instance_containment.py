"""Production paths and deterministic work counts for instances and containers."""

import json
from collections import Counter
from dataclasses import replace

import pytest
from lock_support import CRED, DOOR, A, lock_loader
from test_dnd_ledger_scope import stored_state
from test_lockable_passages import acquire
from test_world_containers import BAG, BOX, container_data
from world_support import setup, submit

from trpg_state_machine import PutItem, StateMachine, StateMachineError, TakeItem
from trpg_state_machine.instance_data import InstanceData
from trpg_state_machine.package import AdventurePackage


def prohibit_templates(monkeypatch, package):
    original = AdventurePackage.__getattribute__
    forbidden = {
        "world_objects",
        "scenes",
        "scene",
        "actors",
        "portable_items",
        "lockable_doors",
        "passages",
        "navigation",
    }

    def read(obj, name):
        if obj is package and name in forbidden:
            raise AssertionError(f"runtime template read: {name}")
        return original(obj, name)

    monkeypatch.setattr(AdventurePackage, "__getattribute__", read)


def test_changed_source_requires_new_runtime_and_rejects_old_binding(tmp_path, context):
    from dnd5e_engine.evaluation_ruleset import ruleset_binding

    source = lock_loader()
    sm, sid, package, db = setup(tmp_path, context, container_data())
    with sm:
        before = stored_state(db)
        item = source.get_item("dagger")
        item.name += " changed"
        row = sm._conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        sm._rules._verify_dnd_binding(package, row)
        assert sm._rules.loader.get_item("dagger").name != item.name
    with StateMachine(db, dnd_loader=source) as changed:
        assert (
            ruleset_binding(changed._rules.loader).data_revision
            != package.ruleset_binding.data_revision
        )
        with pytest.raises(StateMachineError, match="Evaluator data") as error:
            changed.resume_session(sid, package, context=context)
        assert error.value.code == "ruleset_binding_mismatch"
        assert stored_state(db) == before


def test_container_paths_restore_and_ensure_without_templates(tmp_path, context, monkeypatch):
    sm, sid, package, db = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        prohibit_templates(monkeypatch, package)
        for op, key in (
            (PutItem(A, BAG, BOX, "0,0"), "bag"),
            (PutItem(A, CRED, BAG, "0,0"), "key"),
        ):
            assert submit(sm, sid, package, context, op, key).status == "committed"
        assert sm.get_object(sid, actor, BOX, package, context=context)["contents_weight_lb"] == 3
        sm.list_scene_objects(sid, actor, package, context=context)
        sm.get_spatial_view(sid, actor, package, context=context)
        with sm._transaction(write=True):
            assert sm.scene_lifecycle.ensure_scene(sid, A, package) is False
        saved = sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert restored.resume_session(sid, package, context=context) == saved
        assert (
            submit(restored, sid, package, context, TakeItem(A, CRED, BAG, "0,0"), "take").status
            == "committed"
        )
        assert (
            restored.get_object(sid, actor, BOX, package, context=context)["contents_weight_lb"]
            == 2
        )


def test_capability_paths_use_instance_configs(tmp_path, context, monkeypatch):
    from test_world_mechanisms import LEVER, mechanism_data

    from trpg_state_machine import ActivateMechanism, UnlockDoor

    sm, sid, package, _ = setup(tmp_path, context, mechanism_data())
    with sm:
        acquire(sm, sid, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        prohibit_templates(monkeypatch, package)
        assert (
            submit(sm, sid, package, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, ActivateMechanism(A, LEVER, "0,0"), "activate").status
            == "committed"
        )
        assert (
            sm.get_object(sid, actor, LEVER, package, context=context)["mechanism_status"]
            == "spent"
        )
        sm.get_illumination(sid, actor, "0,0", package, context=context)
        sm.save_session(sid, package, context=context)


class CountingRows(dict):
    def __init__(self, rows):
        super().__init__(rows)
        self.visits = 0
        self.scans = 0

    def values(self):
        self.scans += 1
        for row in super().values():
            self.visits += 1
            yield row


@pytest.mark.parametrize("nested", [False, True])
def test_index_build_and_weight_work_linear_for_100_items(tmp_path, context, monkeypatch, nested):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        svc = sm._objects.containers
        base = svc._rows(sid)
        root = dict(base[BOX])
        # Isolate the measured graph while using real persisted instance configs.
        rows = {BOX: root}
        if nested:
            rows[BAG] = {**base[BAG], "container_id": BOX}
        for n in range(100):
            oid = f"object.unit_{n}"
            rows[oid] = {**base[CRED], "object_id": oid, "container_id": BAG if nested else BOX}
        original = sm._world_facts.object_definition
        calls = Counter()

        def definition(session_id, oid):
            calls[oid] += 1
            value = original(session_id, CRED if oid.startswith("object.unit_") else oid)
            return replace(value, object_id=oid)

        monkeypatch.setattr(sm._world_facts, "object_definition", definition)
        counted = CountingRows(rows)
        state = svc.prepare(counted)
        assert svc.subtree(BOX, state) == set(rows)
        assert svc.total_weight(BOX, state, package) == 102 + (2 if nested else 0)
        if nested:
            assert svc.total_weight(BAG, state, package) == 102
        assert svc.total_weight(BOX, state, package) == 102 + (2 if nested else 0)
        assert counted.scans == 1 and counted.visits == len(rows)
        assert calls == Counter({oid: 1 for oid in rows})
        assert len(state.weights) == len(rows)
        # Instrument the original traversal as the comparison, without timing.
        legacy, pending, visited = CountingRows(rows), [BOX], set()
        while pending:
            current = pending.pop()
            visited.add(current)
            pending.extend(r["object_id"] for r in legacy.values() if r["container_id"] == current)
        assert visited == set(rows) and legacy.scans == len(rows)
        assert (legacy.visits, counted.visits) == ((10404, 102) if nested else (10201, 101))


@pytest.mark.parametrize("nested", [False, True])
def test_public_queries_for_100_persisted_contents_use_owner_fields(
    tmp_path, context, monkeypatch, nested
):
    from copy import deepcopy

    from trpg_state_machine.world_containers import ContainmentState

    data = container_data()
    obj = next(o for o in data["scenes"][0]["objects"] if o["object_id"] == CRED)
    item = next(i for i in data["item_transfers"] if i["instance_id"] == CRED)
    profile = next(d for d in data["world_objects"] if d["object_id"] == CRED)
    identities = tuple(f"object.unit_{n}" for n in range(100))
    for oid in identities:
        data["scenes"][0]["objects"].append({**deepcopy(obj), "object_id": oid})
        data["item_transfers"].append({**deepcopy(item), "instance_id": oid})
        data["world_objects"].append(
            {
                **deepcopy(profile),
                "object_id": oid,
                "definition_ref": "definition." + oid.split(".")[1],
            }
        )
    for d in data["world_objects"]:
        for c in d["capabilities"]:
            if c["kind"] == "container":
                c.update(capacity_weight_lb=1000, capacity_volume_cu_ft=1000)
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._transaction(write=True):
            for oid in identities:
                sm._world_facts.transfer_item(
                    sid, sm._world_facts.object(sid, oid), container_id=BAG if nested else BOX
                )
            if nested:
                sm._world_facts.transfer_item(
                    sid, sm._world_facts.object(sid, BAG), container_id=BOX
                )
        prohibit_templates(monkeypatch, package)
        builds = []
        initialize = ContainmentState.__init__

        def counted(self, rows):
            builds.append(len(rows))
            initialize(self, rows)

        monkeypatch.setattr(ContainmentState, "__init__", counted)
        view = sm.get_object(sid, actor, BOX, package, context=context)
        assert view["contents"] == ((BAG,) if nested else tuple(sorted(identities)))
        assert view["contents_weight_lb"] == (102 if nested else 100)
        assert builds == []
        builds.clear()
        views = sm.list_scene_objects(sid, actor, package, context=context)
        assert next(v for v in views if v["object_id"] == BOX) == view
        assert builds == []


def test_candidate_relocation_expires_weights_and_preserves_snapshot(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        svc = sm._objects.containers
        state = svc._rows(sid)
        assert svc.total_weight(BOX, state, package) == 2
        state.relocate(CRED, container_id=BOX, carrier_id=None, scene_id=None, cell=None)
        assert svc.total_weight(BOX, state, package) == 3
        state.relocate(CRED, container_id=BAG)
        assert svc.total_weight(BOX, state, package) == 2
        assert svc.total_weight(BAG, state, package) == 3
        assert svc.total_weight(BAG, svc._rows(sid), package) == 2
        with pytest.raises(TypeError):
            state[CRED]["container_id"] = BOX


@pytest.mark.parametrize("invalid", ["cycle", "missing", "noncontainer"])
def test_invalid_graph_is_not_hidden_by_unknown_weight(tmp_path, context, invalid, monkeypatch):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        svc = sm._objects.containers
        state = svc._rows(sid)
        definition = sm._world_facts.object_definition
        monkeypatch.setattr(
            sm._world_facts,
            "object_definition",
            lambda session_id, oid: replace(definition(session_id, oid), weight_lb=None),
        )
        if invalid == "cycle":
            state.relocate(BAG, container_id=BOX)
            state.relocate(BOX, container_id=BAG)

            def op():
                return svc.total_weight(BOX, state, package)
        elif invalid == "missing":
            state.relocate(BOX, container_id="object.missing")

            def op():
                return svc.audit(state, package, affected={BOX})
        else:
            state.relocate(BAG, container_id=CRED)

            def op():
                return svc.total_weight(CRED, state, package)

        with pytest.raises(StateMachineError) as error:
            op()
        assert error.value.code == (
            "unsupported_object_capability" if invalid == "noncontainer" else "container_cycle"
        )


def test_openable_real_check_after_template_access_is_disabled(tmp_path, context, monkeypatch):
    from test_openable_checks import setup_check

    from trpg_state_machine import PickLock

    sm, sid, package, _ = setup_check(tmp_path, context)
    with sm:
        prohibit_templates(monkeypatch, package)
        assert (
            submit(
                sm, sid, package, context, PickLock(A, DOOR, "lock.outer", CRED, "0,0"), "pick"
            ).status
            == "committed"
        )
        sm.save_session(sid, package, context=context)


def test_spatial_durability_after_template_access_is_disabled(tmp_path, context, monkeypatch):
    from test_object_spatial_service import OBSTACLE, oriented_data
    from test_world_durability import adopt

    from trpg_state_machine.object_durability import _ObjectHPSettlement

    data = oriented_data()
    data["world_objects"][-1]["capabilities"].append(
        {
            "kind": "durability",
            "armor_class": 10,
            "max_hp": 5,
            "initial_hp": 5,
            "damage_profile_ref": "scenario.explicit_settled_damage",
            "damage_threshold": None,
            "destruction_policy_ref": "disable_spatial_policy",
        }
    )
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        before = sm.read_session(sid, package, context=context)
        prohibit_templates(monkeypatch, package)
        adopt(
            sm,
            sid,
            package,
            context,
            _ObjectHPSettlement(sid, OBSTACLE, "damage", before.world_version, 5, 0),
        )
        assert sm._world_facts.object_hp(sid, OBSTACLE) == 0
        assert sm._objects.spatial.project(sid, OBSTACLE, package).movement_blocking_cells == ()
        sm.get_object(sid, before.pc_actor_id, OBSTACLE, package, context=context)
        sm.save_session(sid, package, context=context)


def test_old_save_upgrade_is_explicit_atomic_and_preserves_facts(tmp_path, context, monkeypatch):
    sm, sid, package, db = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        assert (
            submit(sm, sid, package, context, PutItem(A, CRED, BAG, "0,0"), "put").status
            == "committed"
        )
        sm._conn.execute("DELETE FROM instance_configs WHERE session_id=?", (sid,))
        before = stored_state(db)
        with pytest.raises(StateMachineError) as error:
            sm.resume_session(sid, package, context=context)
        assert error.value.code == "incomplete_instance_config" and stored_state(db) == before
        original = InstanceData.put

        def fail(self, session_id, domain, identity, value):
            original(self, session_id, domain, identity, value)
            if domain == "object":
                raise RuntimeError("late upgrade failure")

        with monkeypatch.context() as patch:
            patch.setattr(InstanceData, "put", fail)
            with pytest.raises(RuntimeError):
                sm.upgrade_instance_configs(sid, package, context=context)
        assert stored_state(db) == before
        with pytest.raises(StateMachineError):
            sm.upgrade_instance_configs(
                sid, replace(package, package_digest="sha256:" + "f" * 64), context=context
            )
        sm.upgrade_instance_configs(sid, package, context=context)
        after = stored_state(db)
        derived = {
            "instance_configs",
            "object_spatial_cells",
            "scene_integrity",
            "scene_integrity_dependencies",
        }
        assert {k: v for k, v in after.items() if k not in derived} == {
            k: v for k, v in before.items() if k not in derived
        }
        assert sm._world_facts.object(sid, CRED)["container_id"] == BAG
        prohibit_templates(monkeypatch, package)
        saved = sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert restored.resume_session(sid, package, context=context) == saved


def test_sqlite_direct_contents_uses_existing_index(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        plan = sm._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM objects "
            "WHERE session_id=? AND container_id=? ORDER BY object_id",
            (sid, BOX),
        ).fetchall()
        assert "objects_container" in " ".join(r[3] for r in plan)
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 40
        configs = sm._conn.execute(
            "SELECT config_json FROM instance_configs WHERE domain='object'"
        ).fetchall()
        for (config,) in configs:
            definition = json.loads(json.loads(config)["definition"])
            if definition:
                for cap in definition["fields"]["capabilities"]["tuple"]:
                    assert not any(key.startswith("initial_") for key in cap["fields"])
