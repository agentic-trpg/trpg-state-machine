"""Real public consumers, immutable owner fields and cross-connection recovery."""

import json

import pytest
from lock_support import CRED, A
from test_lockable_passages import acquire
from test_scoped_runtime_validation import grow, measure
from test_world_containers import BAG, BOX, container_data
from test_world_lights import lighting_data
from world_support import command, setup, submit

from trpg_state_machine import (
    PickupItem,
    PutItem,
    SetLightEnabled,
    StateMachine,
    StateMachineError,
    StepActor,
)
from trpg_state_machine.package import canonical_json
from trpg_state_machine.runtime_instances import instance_read
from trpg_state_machine.world_containers import ContainerService, ContainmentState
from trpg_state_machine.world_mechanisms import MechanismService


@pytest.mark.parametrize("failure", ("publish", "partial", "published", "unknown"))
def test_other_connection_recovery_invalidates_failed_publishers_ram(
    tmp_path, context, monkeypatch, failure
):
    first, sid, package, database = setup(tmp_path, context, container_data())
    with first, StateMachine(database, dnd_loader=first._rules.loader) as second:
        acquire(first, sid, package, context)
        actor = first.read_session(sid, package, context=context).pc_actor_id
        old = first.get_object(sid, actor, BOX, package, context=context)
        second.read_session(sid, package, context=context)
        operation = command(first, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "fault")
        publish, commit = first._store.memory.publish, first._store._commit

        def fail_publish(scope):
            if failure == "published":
                publish(scope)
            elif failure == "partial":
                remember = first._store.memory.remember

                def fail_remember(key, value):
                    remember(key, value)
                    raise MemoryError("partial publication")

                with monkeypatch.context() as patch:
                    patch.setattr(first._store.memory, "remember", fail_remember)
                    publish(scope)
            raise MemoryError("publication failed")

        def unknown_commit():
            commit()
            raise OSError("commit acknowledgement lost")

        with monkeypatch.context() as patch:
            if failure == "unknown":
                patch.setattr(first._store, "_commit", unknown_commit)
            else:
                patch.setattr(first._store.memory, "publish", fail_publish)
            with pytest.raises(StateMachineError) as error:
                first.submit_command(operation, package, context=context)
        assert error.value.code == (
            "commit_outcome_unknown" if failure == "unknown" else "committed_publication_pending"
        )
        with pytest.raises(StateMachineError, match="reconciliation"):
            first.get_object(sid, actor, BOX, package, context=context)
        # Recovery itself is read-only: data_version on A cannot detect that B
        # discarded a partially published representation. The shared epoch must.
        second.resume_session(sid, package, context=context)
        current = first.get_object(sid, actor, BOX, package, context=context)
        assert current["contents"] == (CRED,)
        assert old["contents"] == ()
        assert first.submit_command(operation, package, context=context).status == "committed"
        assert first.get_object(sid, actor, BOX, package, context=context) == current


def test_failed_recovery_keeps_all_contexts_paused(tmp_path, context, monkeypatch):
    first, sid, package, database = setup(tmp_path, context, container_data())
    with first, StateMachine(database, dnd_loader=first._rules.loader) as second:
        acquire(first, sid, package, context)
        op = command(first, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "fault")
        with monkeypatch.context() as patch:
            patch.setattr(
                first._store.memory, "publish", lambda scope: (_ for _ in ()).throw(MemoryError())
            )
            with pytest.raises(StateMachineError, match="reconcile RAM"):
                first.submit_command(op, package, context=context)
        with monkeypatch.context() as patch:
            patch.setattr(
                second, "save_session", lambda *a, **k: (_ for _ in ()).throw(ValueError())
            )
            with pytest.raises(ValueError):
                second.resume_session(sid, package, context=context)
        for machine in (first, second):
            with pytest.raises(StateMachineError, match="reconciliation"):
                machine.read_session(sid, package, context=context)
        second.resume_session(sid, package, context=context)
        assert first._world_facts.object(sid, CRED)["container_id"] == BOX


def test_old_reader_cannot_republish_after_read_only_recovery(tmp_path, context, monkeypatch):
    first, sid, package, database = setup(tmp_path, context, container_data())
    with first, StateMachine(database, dnd_loader=first._rules.loader) as second:
        accepted = []
        original = first._store.memory.accept_read

        def accept(scope):
            accepted.append(scope)
            return original(scope)

        monkeypatch.setattr(first._store.memory, "accept_read", accept)
        with first._transaction():
            old = first._world_facts.object(sid, CRED)
            with second._store.reconciliation(sid):
                second.save_session(sid, package, context=context)
            assert old["object_id"] == CRED
        assert accepted == []
        assert ("instance", sid, "object", CRED) not in first._store.memory.values
        assert dict(first._world_facts.object(sid, CRED)) == dict(old)


def test_nested_fields_candidate_savepoint_unknown_and_restart(tmp_path, context):
    sm, sid, package, database = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        submit(sm, sid, package, context, PickupItem(BAG, A, "1,0"), "bag")
        submit(sm, sid, package, context, PutItem(A, BAG, BOX, "0,0"), "nested")
        before = sm.get_object(sid, actor, BOX, package, context=context)
        assert before["contents_weight_lb"] == 2
        submit(sm, sid, package, context, PutItem(A, CRED, BAG, "0,0"), "inside")
        current = sm.get_object(sid, actor, BOX, package, context=context)
        expected = current["contents_weight_lb"]
        assert expected > 2 and before["contents_weight_lb"] == 2
        with sm._transaction(write=True):
            with pytest.raises(ValueError), sm._store.savepoint():
                sm._conn.execute(
                    "UPDATE objects SET container_id=NULL,carrier_id=? "
                    "WHERE session_id=? AND object_id=?",
                    (actor, sid, CRED),
                )
                assert sm._objects.containers.contents_weight(sid, BOX) == 2
                raise ValueError("rollback")
            assert sm._objects.containers.contents_weight(sid, BOX) == expected
        # Controlled instance configuration changes have no public command yet;
        # exercise their actual transaction/change-capture boundary, not a fake cache.
        with sm._transaction(write=True):
            data = sm._world_facts.instances.get(sid, "object", CRED)
            from trpg_state_machine.instance_data import encode

            original_config = canonical_json(encode(data))
            definition = json.loads(data["definition"])
            definition["fields"]["weight_lb"] = None
            data["definition"] = canonical_json(definition)
            sm._conn.execute(
                "UPDATE instance_configs SET config_json=? "
                "WHERE session_id=? AND domain='object' AND instance_id=?",
                (canonical_json(encode(data)), sid, CRED),
            )
            sm.scene_lifecycle._validate_scene(sid, A, package)
        assert (
            sm.get_object(sid, actor, BOX, package, context=context)["contents_weight_lb"] is None
        )
        with sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE instance_configs SET config_json=? "
                "WHERE session_id=? AND domain='object' AND instance_id=?",
                (original_config, sid, CRED),
            )
            sm.scene_lifecycle._validate_scene(sid, A, package)
        sm.unload_session(sid, package, context=context)
        assert (
            sm.get_object(sid, actor, BOX, package, context=context)["contents_weight_lb"]
            == expected
        )
    with StateMachine(database, dnd_loader=sm._rules.loader) as recovered:
        recovered.resume_session(sid, package, context=context)
        result = recovered.get_object(sid, actor, BOX, package, context=context)
        assert result["contents"] == (BAG,) and result["contents_weight_lb"] == expected


def test_public_weight_does_not_build_candidate_or_scan_a_key(tmp_path, context, monkeypatch):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        submit(sm, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "put")
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert not hasattr(ContainmentState, "runtime_key")

        def forbidden(*args, **kwargs):
            raise AssertionError("public read reassembled a candidate")

        monkeypatch.setattr(sm._objects.containers, "_rows", forbidden)
        monkeypatch.setattr(sm._objects.containers, "_weight", forbidden)
        first = sm.get_object(sid, actor, BOX, package, context=context)
        with measure(sm) as (counts, _):
            for _ in range(4):
                assert sm.get_object(sid, actor, BOX, package, context=context) == first
        assert counts["sql_selects"] == counts["sql_rows"] == 0


@pytest.mark.parametrize("growth", (0, 120))
def test_fixed_weight_scope_counters_do_not_grow(tmp_path, context, monkeypatch, growth):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        if growth:
            grow(sm, sid, growth)
        acquire(sm, sid, package, context)
        submit(sm, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "put")
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        sm.get_object(sid, actor, BOX, package, context=context)
        calls = []
        original = ContainerService.contents_weight.__wrapped__

        @instance_read("object", "container", "contents_weight")
        def counted(self, *args, **kwargs):
            calls.append(1)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(ContainerService, "contents_weight", counted)
        before = dict(sm._store.memory.stats)
        with measure(sm) as (sql, _):
            for _ in range(4):
                sm.get_object(sid, actor, BOX, package, context=context)
        assert calls == [] and sql["sql_rows"] == sql["sql_selects"] == 0
        assert sm._store.memory.stats["model_parses"] == before["model_parses"]
        assert sm._store.memory.stats["json_parses"] == before["json_parses"]


def test_empty_mechanism_scene_index_survives_commands_and_unrelated_state(
    tmp_path, context, monkeypatch
):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    builds = []
    original = MechanismService._scene_index.__wrapped__

    @instance_read("scene", "mechanisms", "index")
    def counted(self, *args):
        builds.append(args)
        return original(self, *args)

    monkeypatch.setattr(MechanismService, "_scene_index", counted)
    with sm:
        assert (
            submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "step1").status
            == "committed"
        )
        count = len(builds)
        assert count == 1
        assert (
            submit(sm, sid, package, context, StepActor(A, "1,0", "0,0"), "step2").status
            == "committed"
        )
        assert len(builds) == count
        owner = sm._store.memory.values[("instance", sid, "scene", A)][0]
        assert owner.mechanisms.index[0].value == {}


def test_cell_light_uses_one_cell_and_keeps_fixed_snapshots(tmp_path, context, monkeypatch):
    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.read_noncombat_snapshot(sid, package, context=context)
        submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on")
        light = sm._objects.lights
        project, calls = light._illuminate_cell, []

        def counted(*args, **kwargs):
            calls.append(args[-1])
            return project(*args, **kwargs)

        monkeypatch.setattr(light, "_illuminate_cell", counted)
        assert (
            sm.get_illumination(sid, actor, "1,0", package, context=context)["illumination"]
            == "bright"
        )
        assert calls == ["1,0"]
        sm.get_illumination(sid, actor, "1,0", package, context=context)
        assert len(calls) == 1
        assert (
            old.scene_state.grid.lighting.get("1,0", old.scene_state.grid.default_lighting)
            == "dark"
        )
        owner = sm._store.memory.values[("instance", sid, "scene", A)][0]
        assert owner.lighting.cells and owner.lighting.sources and owner.spatial.mechanical_map


def test_object_visibility_uses_target_light_and_shares_immutable_geometry(
    tmp_path, context, monkeypatch
):
    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on")

        def forbidden(*args, **kwargs):
            raise AssertionError("single-object visibility projected the whole map")

        monkeypatch.setattr(sm._objects.lights, "current_project", forbidden)
        view = sm.get_object(sid, actor, BOX, package, context=context)
        assert view["object_id"] == BOX
        base = sm._world.grid.reader.prepared_scene(sid, A, package).topology
        local = sm._world.grid.base_topology(sid, A, package, cells=("1,0",))
        assert local._walls is base._walls and local._blocked is base._blocked
        assert local.light_on_cell("1,0") == "bright"
        assert base.light_on_cell("1,0") == "dark"
        with pytest.raises(TypeError):
            local._lighting["1,0"] = "dark"


def test_mechanism_configuration_change_updates_the_public_event_index(tmp_path, context):
    from test_mechanism_service import extended_data, trigger
    from test_world_mechanisms import LEVER

    from trpg_state_machine.instance_data import encode

    sm, sid, package, _ = setup(
        tmp_path, context, extended_data(triggers=[trigger("enter_region", cells=["1,0"])])
    )
    with sm:
        assert (
            submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "first").status
            == "committed"
        )
        key = ("instance", sid, "scene", A)
        before = sm._store.memory.values[key][0].mechanisms.index[0].value
        assert next(iter(before.values()))[0][2].cells == ("1,0",)
        with sm._transaction(write=True):
            data = sm._world_facts.instances.get(sid, "object", LEVER)
            definition = json.loads(data["definition"])
            mechanism = next(
                c for c in definition["fields"]["capabilities"]["tuple"] if c["type"] == "Mechanism"
            )
            mechanism["fields"]["triggers"]["tuple"][0]["fields"]["cells"] = {"tuple": ["2,0"]}
            data["definition"] = canonical_json(definition)
            sm._conn.execute(
                "UPDATE instance_configs SET config_json=? "
                "WHERE session_id=? AND domain='object' AND instance_id=?",
                (canonical_json(encode(data)), sid, LEVER),
            )
            sm.scene_lifecycle._validate_scene(sid, A, package)
        assert (
            submit(sm, sid, package, context, StepActor(A, "1,0", "0,0"), "second").status
            == "committed"
        )
        current = sm._store.memory.values[key][0].mechanisms.index[0].value
        assert next(iter(current.values()))[0][2].cells == ("2,0",)
        assert next(iter(before.values()))[0][2].cells == ("1,0",)


def test_area_validation_is_local_and_does_not_compose_environment(tmp_path, context, monkeypatch):
    from trpg_state_machine import EnvironmentArea

    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:

        def forbidden(*args, **kwargs):
            raise AssertionError("area validation enumerated scenes or composed weather")

        monkeypatch.setattr(sm._world_facts.instances, "scenes", forbidden)
        monkeypatch.setattr(sm.environment, "_inputs", forbidden)
        with sm._transaction():
            assert sm.environment._area(sid, A, EnvironmentArea(("1,0",)), package) == ("1,0",)
            with pytest.raises(StateMachineError, match="bounds"):
                sm.environment._area(sid, A, EnvironmentArea(("999,0",)), package)
            with pytest.raises(StateMachineError, match="Unknown"):
                sm.environment._area(sid, "scene.absent", EnvironmentArea(), package)
