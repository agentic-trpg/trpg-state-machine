"""Internal synthetic settlements prove application, not RE object attacks."""

from dataclasses import replace

import pytest
from lock_support import DOOR
from world_support import setup, world_data

from trpg_state_machine import ObjectState, SetObjectState, StateMachine, StateMachineError
from trpg_state_machine._execution import _WorldEffectKind, _WorldEffectOrigin
from trpg_state_machine.commands import world_command_from_data
from trpg_state_machine.object_durability import _ObjectHPSettlement


def durability_data():
    d = world_data()
    d["world_objects"][0]["capabilities"].append(
        {
            "kind": "durability",
            "armor_class": 19,
            "max_hp": 20,
            "initial_hp": 20,
            "damage_profile_ref": "scenario.explicit_settled_damage",
            "damage_threshold": None,
            "destruction_policy_ref": "disable_preserve_obstruction",
        }
    )
    return d


def adopt(sm, sid, p, ctx, result, *, fail=False):
    # Trusted INTERNAL fixture, emulates the shared commit owner, not a player API.
    with sm._transaction(write=True):
        row = sm._session(sid, p, ctx)
        events = sm._durability._apply_admitted(
            row, p, result, origin=_WorldEffectOrigin(_WorldEffectKind.SYNTHETIC, "fixture.settled")
        )
        if events:
            sm._session_runtime.set_world_version(sid, row["world_version"] + 1)
            sm._append_events(
                sid,
                row["world_version"] + 1,
                events,
                source_command_id=result.resolution_id,
                package=p,
            )
        if fail:
            raise RuntimeError("injected failure")


def test_settlement_replay_conflict_destruction_rollback_and_restore(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, durability_data())
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        result = _ObjectHPSettlement(sid, DOOR, "resolution.fixture", 0, 20, 12)
        with pytest.raises(RuntimeError):
            adopt(sm, sid, p, context, result, fail=True)
        assert sm.get_object(sid, actor, DOOR, p, context=context)["current_hp"] == 20
        adopt(sm, sid, p, context, result)
        adopt(sm, sid, p, context, result)
        assert sm.read_session(sid, p, context=context).world_version == 1
        with pytest.raises(StateMachineError, match="reused"):
            adopt(sm, sid, p, context, replace(result, current_hp=0))
        with pytest.raises(StateMachineError, match="stale"):
            adopt(sm, sid, p, context, replace(result, resolution_id="stale"))
        with pytest.raises(StateMachineError):
            adopt(sm, sid, p, context, replace(result, session_id="foreign"))
        for hp in (-1, 21, True):
            with pytest.raises(StateMachineError):
                adopt(sm, sid, p, context, _ObjectHPSettlement(sid, DOOR, "bad", 1, 12, hp))
        adopt(sm, sid, p, context, _ObjectHPSettlement(sid, DOOR, "destroy", 1, 12, 0))
        view = sm.get_object(sid, actor, DOOR, p, context=context)
        assert view["destroyed"] and view["effective_capabilities"] == ()
        from world_support import submit

        assert (
            submit(
                sm,
                sid,
                p,
                context,
                SetObjectState("scene.platform", DOOR, ObjectState.OPEN),
                "open.destroyed",
            ).error_code
            == "object_destroyed"
        )
    from lock_support import lock_loader

    with StateMachine(db, dnd_loader=lock_loader()) as recovered:
        assert recovered.get_object(sid, actor, DOOR, p, context=context)["current_hp"] == 0
        adopt(recovered, sid, p, context, result)


def test_no_public_damage_backdoor():
    with pytest.raises(StateMachineError):
        world_command_from_data(
            {
                "session_id": "s",
                "command_id": "c",
                "actor_id": "a",
                "command_type": "world.damage_object",
                "payload": {"amount": 5, "validated": True},
                "expected_world_version": 0,
                "schema_version": "sm-command/1",
            }
        )


def test_unknown_threshold_is_preserved_and_not_reinterpreted_as_no_threshold(tmp_path, context):
    d = durability_data()
    d["world_objects"][0]["capabilities"][-1]["damage_threshold"] = "unknown"
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert (
            sm.get_object(sid, actor, DOOR, p, context=context)["durability"]["damage_threshold"]
            == "unknown"
        )
