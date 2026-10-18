from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from lock_support import CRED, DOOR, A, lock_loader
from test_world_mechanisms import LEVER, mechanism_data
from world_support import command, setup, submit

from trpg_state_machine import (
    ActivateMechanism,
    AdvanceGameTime,
    CheckMechanism,
    DropItem,
    ObjectState,
    PickupItem,
    PutItem,
    ServiceMechanism,
    SetObjectState,
    StateMachine,
    StateMachineError,
    StepActor,
    TakeItem,
    TimeBinding,
)
from trpg_state_machine.package import _validate


def ordered_data(*, conflict=False):
    d = mechanism_data()
    d["lockable_doors"] = []
    for oid, state in [("object.panel_a", "closed"), ("object.panel_b", "open")]:
        obj = deepcopy(d["scenes"][0]["objects"][-1])
        obj.update(object_id=oid, state=state, initial_cell="2,1")
        d["scenes"][0]["objects"].append(obj)
        definition = deepcopy(d["world_objects"][-1])
        definition.update(
            object_id=oid,
            definition_ref="definition." + oid.split(".")[1],
            capabilities=[
                {"kind": "openable"},
                {
                    "kind": "spatial",
                    "footprint": [[0, 0]],
                    "mobility_policy": "fixed",
                    "open_blocks_movement": False,
                    "closed_blocks_movement": True,
                    "destroyed_blocks_movement": False,
                },
            ],
        )
        d["world_objects"].append(definition)
    c = next(x for x in d["world_objects"] if x["object_id"] == LEVER)["capabilities"][0]
    c["actions"] = [
        {
            "kind": "set_open_state",
            "target_object_id": "object.panel_a",
            "value": "closed" if conflict else "open",
        },
        {"kind": "set_open_state", "target_object_id": "object.panel_b", "value": "closed"},
    ]
    return d


@pytest.mark.parametrize("conflict", [False, True])
def test_mech01_ordered_candidate_query_and_commit(tmp_path, context, conflict):
    sm, sid, p, _ = setup(tmp_path, context, ordered_data(conflict=conflict))
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        op = ActivateMechanism(A, LEVER, "0,0")
        before = [tuple(r) for r in sm._conn.execute("SELECT * FROM objects")]
        q = sm.query_world_action(sid, actor, op, p, context=context)
        assert q.status == ("unavailable" if conflict else "available"), q
        assert [tuple(r) for r in sm._conn.execute("SELECT * FROM objects")] == before
        receipt = submit(sm, sid, p, context, op, "ordered")
        assert receipt.status == ("rejected" if conflict else "committed"), receipt
        if conflict:
            assert [tuple(r) for r in sm._conn.execute("SELECT * FROM objects")] == before
        else:
            assert sm._world_facts.object(sid, "object.panel_a")["state"] == "open"
            assert sm._world_facts.object(sid, "object.panel_b")["state"] == "closed"


def method(identity, operation=None, **kwargs):
    return dict(
        method_id=identity,
        operation=operation or identity,
        required_tool=None,
        ability=None,
        skill=None,
        dc=None,
        trigger_on_failure=None,
        **kwargs,
    )


def config(**changes):
    c = dict(
        kind="mechanism",
        schema_version="mechanism/2",
        initial_status="armed",
        operations=[
            dict(
                operation_id="up",
                consume_on_noop=False,
                actions=[dict(kind="set_light_enabled", target_object_id=CRED, value=True)],
            ),
            dict(
                operation_id="down",
                consume_on_noop=True,
                actions=[dict(kind="set_light_enabled", target_object_id=CRED, value=False)],
            ),
        ],
        triggers=[],
        methods=[method(m) for m in ("disarm", "block", "unblock", "prepare", "reset")],
        max_uses=None,
        reset_policy="none",
        reset_delay_seconds=None,
        frequency="unlimited",
        placement_policy="accessible",
        components=[],
        destruction_policy="disable",
    )
    c.update(changes)
    return c


def extended_data(**changes):
    d = mechanism_data()
    d["manifest"]["schema_version"] = "state-machine-foundation/10"
    d["lockable_doors"] = []
    d["world_objects"][-1]["capabilities"] = [config(**changes)]
    return d


def trigger(kind, **changes):
    t = dict(
        kind=kind,
        scene_id=A,
        operation_id="up",
        cells=[],
        target_object_id=None,
        event_type=None,
        succeeded=None,
    )
    t.update(changes)
    return t


def test_reusable_noop_adoption_replay_conflict_and_recovery(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, extended_data(max_uses=3))
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        up = ActivateMechanism(A, LEVER, "0,0", "up")
        cmd = command(sm, sid, p, context, up, "up")
        before_rng = sm.read_session(sid, p, context=context).rng
        r = sm.submit_command(cmd, p, context=context)
        assert r.status == "committed", r
        assert sm.submit_command(cmd, p, context=context) == r
        state = sm._world_facts.mechanism_state(sid, LEVER)
        assert (state.status, state.remaining_uses, state.activation_count) == ("armed", 2, 1)
        noop = submit(sm, sid, p, context, up, "noop")
        assert noop.status == "committed" and not noop.event_seqs
        assert sm._world_facts.mechanism_state(sid, LEVER) == state
        stale = replace(cmd, command_id="stale")
        assert sm.submit_command(stale, p, context=context).status == "conflict"
        with pytest.raises(StateMachineError, match="different content"):
            sm.submit_command(
                replace(cmd, payload=replace(up, operation_id="down")), p, context=context
            )
        for key in ("down1", "down2"):
            assert (
                submit(sm, sid, p, context, replace(up, operation_id="down"), key).status
                == "committed"
            )
        assert sm._world_facts.mechanism_state(sid, LEVER).remaining_uses == 0
        assert submit(sm, sid, p, context, up, "empty").error_code == "mechanism_spent"
        assert sm.read_session(sid, p, context=context).rng == before_rng
        assert sm._conn.execute("SELECT count(*) FROM mechanism_activations").fetchone()[0] == 3
        assert (
            len(
                {
                    e.payload["activation_id"]
                    for e in sm.read_events(sid, p, context=context)
                    if e.event_type == "mechanism.activated"
                }
            )
            == 3
        )
        assert (
            sm.query_world_action(
                sid, actor, replace(up, operation_id="arbitrary"), p, context=context
            ).status
            == "unavailable"
        )
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert restored.submit_command(cmd, p, context=context) == r
        assert restored._world_facts.mechanism_state(sid, LEVER).status == "spent"


def test_lifecycle_manual_reset_block_resume_and_permanent_disarm(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, extended_data(reset_policy="manual", max_uses=2))
    with sm:
        up = ActivateMechanism(A, LEVER, "0,0", "up")
        assert submit(sm, sid, p, context, up, "up1").status == "committed"
        for m in ("block", "unblock", "reset"):
            assert (
                submit(sm, sid, p, context, ServiceMechanism(A, LEVER, "0,0", m), m).status
                == "committed"
            )
        assert sm._world_facts.mechanism_state(sid, LEVER).status == "armed"
        assert (
            submit(sm, sid, p, context, replace(up, operation_id="down"), "down").status
            == "committed"
        )
        assert (
            submit(
                sm, sid, p, context, ServiceMechanism(A, LEVER, "0,0", "reset"), "empty"
            ).error_code
            == "mechanism_exhausted"
        )
        assert (
            submit(
                sm, sid, p, context, ServiceMechanism(A, LEVER, "0,0", "disarm"), "disarm"
            ).status
            == "committed"
        )
        assert submit(sm, sid, p, context, up, "disarmed").error_code == "mechanism_disarmed"


def test_region_entry_not_dwell_and_actor_frequency(tmp_path, context):
    d = extended_data(
        frequency="once_per_actor", triggers=[trigger("enter_region", cells=["1,0", "2,0"])]
    )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        cmd = command(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "enter")
        r = sm.submit_command(cmd, p, context=context)
        assert r.status == "committed", r
        assert sm.submit_command(cmd, p, context=context) == r
        assert sm._world_facts.light_state(sid, CRED)["enabled"]
        for key, a, b in [
            ("dwell", "1,0", "2,0"),
            ("exit", "2,0", "2,1"),
            ("reenter", "2,1", "2,0"),
        ]:
            assert submit(sm, sid, p, context, StepActor(A, a, b), key).status == "committed"
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
        assert sm._conn.execute("SELECT count(*) FROM mechanism_activations").fetchone()[0] == 1


def test_object_operation_trigger_and_chain_rollback(tmp_path, context):
    d = extended_data(
        triggers=[
            trigger("object_operation", target_object_id=DOOR, event_type="object.state_changed")
        ]
    )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        op = SetObjectState(A, DOOR, ObjectState.OPEN)
        cmd = command(sm, sid, p, context, op, "open")
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_adopt BEFORE INSERT ON mechanism_activations "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(cmd, p, context=context)
        assert sm._world_facts.object(sid, DOOR)["state"] == "closed"
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
        assert sm.get_command_receipt(sid, "open", p, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_adopt")
        r = sm.submit_command(cmd, p, context=context)
        assert r.status == "committed", r
        assert sm.submit_command(cmd, p, context=context) == r
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1


@pytest.mark.parametrize("dc,success", [(0, True), (100, False)])
def test_real_re_disarm_and_configured_failure_activation(tmp_path, context, dc, success):
    m = method("disarm")
    m.update(ability="dex", skill="sleight_of_hand", dc=dc, trigger_on_failure="up")
    sm, sid, p, db = setup(tmp_path, context, extended_data(methods=[m]))
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        op = CheckMechanism(A, LEVER, "0,0", "disarm")
        before = sm.read_session(sid, p, context=context)
        assert sm.query_world_action(sid, actor, op, p, context=context).status == "available"
        cmd = command(sm, sid, p, context, op, "check")
        r = sm.submit_command(cmd, p, context=context)
        assert r.status == "committed", r
        assert sm.submit_command(cmd, p, context=context) == r
        state = sm._world_facts.mechanism_state(sid, LEVER)
        assert state.status == ("disarmed" if success else "armed")
        assert state.activation_count == (0 if success else 1)
        assert sm._world_facts.light_state(sid, CRED)["enabled"] is not success
        assert (
            sm.read_session(sid, p, context=context).rng.rng_version == before.rng.rng_version + 1
        )
        check = next(
            e
            for e in sm.read_events(sid, p, context=context)
            if e.event_type == "rules.check_rolled"
        )
        assert check.payload["engine_event"]["succeeded"] is success
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert restored.submit_command(cmd, p, context=context) == r


def test_adopted_check_result_triggers_without_model_or_fake_result(tmp_path, context):
    from spatial_support import create
    from test_openable_checks import check_data

    from trpg_state_machine import ClearJam

    d, loader = check_data(dc=0, jammed=True)
    mech = extended_data(triggers=[trigger("check_result", target_object_id=DOOR, succeeded=True)])
    d["manifest"]["schema_version"] = "state-machine-foundation/10"
    d["scenes"][0]["objects"].append(mech["scenes"][0]["objects"][-1])
    d["world_objects"].append(mech["world_objects"][-1])
    # Switch the effect to an existing openable to avoid creating a light definition.
    d["world_objects"][-1]["capabilities"][0]["operations"][0]["actions"] = [
        dict(kind="set_open_state", target_object_id=DOOR, value="closed")
    ]
    # The other declared operation also needs an existing supported target.
    d["world_objects"][-1]["capabilities"][0]["operations"][1]["actions"] = [
        dict(kind="set_open_state", target_object_id=DOOR, value="closed")
    ]
    d["world_objects"][-1]["capabilities"][0]["operations"][0]["consume_on_noop"] = True
    p = _validate(d)
    with StateMachine(tmp_path / "check_trigger.db", dnd_loader=loader) as sm:
        sid = create(sm, p, context)
        r = submit(sm, sid, p, context, ClearJam(A, DOOR, "free-jam", None, "0,0"), "check")
        assert r.status == "committed", r
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1


def test_seconds_reset_preserves_block_and_legacy_time_rejected(tmp_path, context):
    d = extended_data(reset_policy="after_seconds", reset_delay_seconds=5)
    d["time_binding"] = asdict(TimeBinding())
    for actor in d["actors"]:
        actor.pop("npc_state", None)
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        ctx = replace(context, can_advance_time=True)
        assert (
            submit(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0", "up"), "up").status
            == "committed"
        )
        assert sm._world_facts.mechanism_state(sid, LEVER).reset_at_tick == 5
        assert (
            submit(sm, sid, p, context, ServiceMechanism(A, LEVER, "0,0", "block"), "block").status
            == "committed"
        )
        assert submit(sm, sid, p, ctx, AdvanceGameTime(0, 5), "time").status == "committed"
        state = sm._world_facts.mechanism_state(sid, LEVER)
        assert (state.status, state.resume_status, state.reset_at_tick) == (
            "blocked",
            "armed",
            None,
        )
        assert (
            submit(
                sm, sid, p, context, ServiceMechanism(A, LEVER, "0,0", "unblock"), "unblock"
            ).status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0", "down"), "down").status
            == "committed"
        )
    del d["time_binding"]
    p2 = tmp_path / "legacy"
    p2.mkdir()
    sm, sid, p, _ = setup(p2, context, d)
    with sm:
        assert (
            submit(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0", "up"), "up").error_code
            == "unsupported_time_protocol"
        )


def test_portable_same_identity_stow_deploy_prepare_disarm_recover(tmp_path, context):
    from test_world_containers import BOX, container_data

    d = container_data()
    mech = extended_data(placement_policy="ground_prepared", initial_status="unprepared")
    d["manifest"]["schema_version"] = "state-machine-foundation/10"
    obj = deepcopy(mech["scenes"][0]["objects"][-1])
    obj["movable"] = False
    d["scenes"][0]["objects"].append(obj)
    definition = deepcopy(mech["world_objects"][-1])
    definition.update(volume_cu_ft=1, fit_profile="station.small", content_kind="station.unit")
    # Container fixture has no emitter. Consequences stay on the existing shared door.
    for op in definition["capabilities"][0]["operations"]:
        op["actions"] = [dict(kind="set_open_state", target_object_id=DOOR, value="closed")]
        op["consume_on_noop"] = True
    d["world_objects"].append(definition)
    item = deepcopy(d["item_transfers"][0])
    item["instance_id"] = LEVER
    d["item_transfers"].append(item)
    sm, sid, p, db = setup(tmp_path, context, d)
    with sm:
        steps = [
            PickupItem(LEVER, A, "1,0"),
            PutItem(A, LEVER, BOX, "0,0"),
            TakeItem(A, LEVER, BOX, "0,0"),
            DropItem(LEVER, A, "1,0"),
        ]
        for i, op in enumerate(steps):
            r = submit(sm, sid, p, context, op, "move" + str(i))
            assert r.status == "committed", r
        assert sm._world_facts.mechanism_state(sid, LEVER).status == "unprepared"
        assert (
            submit(
                sm, sid, p, context, ServiceMechanism(A, LEVER, "0,0", "prepare"), "prepare"
            ).status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0", "up"), "fire").status
            == "committed"
        )
        assert (
            submit(
                sm, sid, p, context, ServiceMechanism(A, LEVER, "0,0", "disarm"), "disarm"
            ).status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, PickupItem(LEVER, A, "1,0"), "recover").status
            == "committed"
        )
        assert sm._world_facts.mechanism_state(sid, LEVER).status == "disarmed"
        assert (
            sm._conn.execute("SELECT count(*) FROM objects WHERE object_id=?", (LEVER,)).fetchone()[
                0
            ]
            == 1
        )
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert restored._world_facts.mechanism_state(sid, LEVER).status == "disarmed"


@pytest.mark.parametrize(
    "change",
    [
        dict(frequency="per_turn"),
        dict(reset_policy="turn"),
        dict(destruction_policy="repair"),
        dict(script="eval"),
    ],
)
def test_unsupported_definition_is_explicit(change):
    with pytest.raises(StateMachineError):
        _validate(extended_data(**change))


def test_light_after_stage_only_and_stable_mechanism_order(tmp_path, context):
    from trpg_state_machine import SetLightEnabled

    d = extended_data(
        triggers=[
            trigger("object_operation", target_object_id=CRED, event_type="object.light_changed")
        ]
    )
    c = d["world_objects"][-1]["capabilities"][0]
    c["operations"][0]["actions"] = [
        dict(kind="set_open_state", target_object_id=DOOR, value="closed")
    ]
    c["operations"][0]["consume_on_noop"] = True
    obj, definition = deepcopy(d["scenes"][0]["objects"][-1]), deepcopy(d["world_objects"][-1])
    obj["object_id"] = "object.aaa"
    definition.update(object_id="object.aaa", definition_ref="definition.aaa")
    d["scenes"][0]["objects"].append(obj)
    d["world_objects"].append(definition)
    # Place the emitter in contact range; use two stage events from the real light path.
    next(o for o in d["scenes"][0]["objects"] if o["object_id"] == CRED)["initial_cell"] = "1,0"
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        r = submit(sm, sid, p, context, SetLightEnabled(A, CRED, "0,0", True), "light")
        assert r.status == "committed", r
        adopted = [
            e.payload["object_id"]
            for e in sm.read_events(sid, p, context=context)
            if e.event_type == "mechanism.activated"
        ]
        assert adopted == ["object.aaa", LEVER]
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1


def test_bounded_recurrent_link_cascade_rolls_back_every_fact(tmp_path, context):
    d = extended_data(
        triggers=[
            trigger(
                "object_operation",
                target_object_id=CRED,
                event_type="object.light_changed",
                operation_id="down",
            )
        ]
    )
    # Two instances alternately turn the emitter off and on through declared actions.
    obj, definition = deepcopy(d["scenes"][0]["objects"][-1]), deepcopy(d["world_objects"][-1])
    obj["object_id"] = "object.other"
    definition.update(object_id="object.other", definition_ref="definition.other")
    definition["capabilities"][0]["triggers"][0]["operation_id"] = "up"
    d["scenes"][0]["objects"].append(obj)
    d["world_objects"].append(definition)
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        r = submit(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0", "up"), "loop")
        assert r.error_code == "event_cascade_limit", r
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
        assert sm._conn.execute("SELECT count(*) FROM mechanism_activations").fetchone()[0] == 0


def test_directional_jam_rechecks_candidate_state_on_repeated_target(tmp_path, context):
    d = ordered_data()
    d["manifest"]["schema_version"] = "state-machine-foundation/10"
    a = next(o for o in d["scenes"][0]["objects"] if o["object_id"] == "object.panel_a")
    a["state"] = "open"
    profile = next(o for o in d["world_objects"] if o["object_id"] == "object.panel_a")
    profile["capabilities"][0].update(initial_jammed=True, jam_policy="prevent_opening")
    c = next(o for o in d["world_objects"] if o["object_id"] == LEVER)["capabilities"][0]
    c["actions"] = [
        dict(kind="set_open_state", target_object_id="object.panel_a", value=v)
        for v in ("closed", "open")
    ]
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        op = ActivateMechanism(A, LEVER, "0,0")
        q = sm.query_world_action(sid, actor, op, p, context=context)
        assert q.reason == "object_jammed"
        assert submit(sm, sid, p, context, op, "jam").error_code == "object_jammed"
        assert sm._world_facts.object(sid, "object.panel_a")["state"] == "open"


def test_deterministic_method_requires_matching_accessible_owned_tool(tmp_path, context):
    from test_lockable_passages import acquire

    m = method("block")
    m["required_tool"] = "amulet"
    sm, sid, p, _ = setup(tmp_path, context, extended_data(methods=[m]))
    with sm:
        op = ServiceMechanism(A, LEVER, "0,0", "block", CRED)
        assert submit(sm, sid, p, context, op, "no.tool").error_code == "tool_unavailable"
        acquire(sm, sid, p, context)
        assert submit(sm, sid, p, context, op, "tool").status == "committed"
        assert sm._world_facts.mechanism_state(sid, LEVER).status == "blocked"


@pytest.mark.parametrize("failure", ["technical", "race", "storage"])
def test_re_check_faults_are_not_character_failure(tmp_path, context, monkeypatch, failure):
    m = method("disarm")
    m.update(ability="dex", skill="sleight_of_hand", dc=100, trigger_on_failure="up")
    sm, sid, p, db = setup(tmp_path, context, extended_data(methods=[m]))
    with sm:
        before = sm.read_session(sid, p, context=context).rng
        cmd = command(sm, sid, p, context, CheckMechanism(A, LEVER, "0,0", "disarm"), "check")
        evaluate = sm._rules._evaluate_dnd

        async def injected(request):
            assert sm._store._scope is None  # No read/write transaction spans RE evaluation.
            if failure == "technical":
                raise RuntimeError("engine fault")
            result = await evaluate(request)
            if failure == "race":
                with StateMachine(db, dnd_loader=lock_loader()) as other:
                    assert (
                        submit(
                            other,
                            sid,
                            p,
                            context,
                            SetObjectState(A, DOOR, ObjectState.OPEN),
                            "concurrent",
                        ).status
                        == "committed"
                    )
            return result

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", injected)
        if failure == "storage":
            sm._conn.execute(
                "CREATE TEMP TRIGGER fail_receipt BEFORE INSERT ON command_receipts "
                "BEGIN SELECT RAISE(ABORT,'fault'); END"
            )
        if failure == "race":
            assert sm.submit_command(cmd, p, context=context).status == "conflict"
        else:
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(cmd, p, context=context)
            assert error.value.code == (
                "storage_error" if failure == "storage" else "rule_evaluation_failed"
            )
            assert sm.get_command_receipt(sid, "check", p, context=context) is None
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
        assert sm.read_session(sid, p, context=context).rng == before


def test_component_destruction_updates_lifecycle_in_same_commit_and_rolls_back(tmp_path, context):
    from test_world_durability import adopt, durability_data

    from trpg_state_machine.object_durability import _ObjectHPSettlement

    d = extended_data(components=[DOOR])
    d["world_objects"][0]["capabilities"].append(
        durability_data()["world_objects"][0]["capabilities"][-1]
    )
    sm, sid, p, db = setup(tmp_path, context, d)
    with sm:
        settlement = _ObjectHPSettlement(sid, DOOR, "destroy", 0, 20, 0)
        with pytest.raises(RuntimeError):
            adopt(sm, sid, p, context, settlement, fail=True)
        assert sm._world_facts.mechanism_state(sid, LEVER).status == "armed"
        assert sm._world_facts.object_hp(sid, DOOR) == 20
        adopt(sm, sid, p, context, settlement)
        adopt(sm, sid, p, context, settlement)
        assert sm._world_facts.mechanism_state(sid, LEVER).status == "broken"
        assert (
            submit(
                sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0", "up"), "broken"
            ).error_code
            == "mechanism_broken"
        )
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert restored._world_facts.mechanism_state(sid, LEVER).status == "broken"


def test_explicit_open_operation_selector_does_not_match_close(tmp_path, context):
    d = extended_data(
        triggers=[
            trigger(
                "object_operation",
                target_object_id=DOOR,
                event_type="object.state_changed",
                value="open",
            )
        ]
    )
    d["world_objects"][-1]["capabilities"][0]["operations"][0]["consume_on_noop"] = True
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        for key, state in [("open", ObjectState.OPEN), ("close", ObjectState.CLOSED)]:
            assert (
                submit(sm, sid, p, context, SetObjectState(A, DOOR, state), key).status
                == "committed"
            )
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1


def test_cross_scene_region_hook_uses_private_motion_and_preserves_event_payload(tmp_path, context):
    from lock_support import B

    from trpg_state_machine import EnterScene

    d = extended_data(triggers=[trigger("enter_region", scene_id=B, cells=["0,0"])])
    lever = d["scenes"][0]["objects"].pop()
    lever["initial_cell"] = "2,1"
    d["scenes"][1]["objects"].append(lever)
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        assert (
            submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open").status
            == "committed"
        )
        receipt = submit(sm, sid, p, context, EnterScene(B), "cross")
        assert receipt.status == "committed", receipt
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
        arrival = next(
            e
            for e in sm.read_events(sid, p, context=context)
            if e.event_type == "scene.entered" and e.event_seq in receipt.event_seqs
        )
        assert arrival.payload == {"scene_id": B}
