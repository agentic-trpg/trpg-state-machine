"""Public long rest with a pinned evaluator, durable time and recovery."""

from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd_support import create
from item_support import item_data
from test_cure_wounds import cure_loader

from trpg_state_machine import (
    AdvanceGameTime,
    LongRest,
    StateMachine,
    StateMachineError,
    TimeBinding,
    TypedCommand,
    world_command_from_data,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.time_coordination import DueResult


def rest_package(*, timed=True, hp=2):
    """Author a canonical first-level Cleric with explicit HP and resource facts."""
    loader = cure_loader()
    data = item_data()
    del data["rule_bindings"]["combat_foundation"]
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    if timed:
        data["time_binding"] = asdict(TimeBinding())
        for person in data["actors"]:
            person.pop("npc_state", None)
    character = data["pc_templates"][0]["character"]
    character["inventory"] = []
    character["hp"]["current"] = hp
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/20"
        pc = snapshot["character_states"][0]
        pc.update(
            class_slug="cleric",
            classes={"cleric": 1},
            granted_features=["spellcasting", "divine-order"],
            character_level=1,
            hp_current=hp,
            spellcasting_ability="wis",
            spell_slots={1: 0},
            carried_item_slugs=[],
            weapon_in_hands=None,
            weapon_grip="none",
        )
        for actor in snapshot["character_states"]:
            actor.pop("spells_known", None)
        snapshot["inventory_state"]["entries"] = []
        snapshot["casting_state"] = {"actors": []}
        snapshot["resource_state"] = {
            "pools": [
                dict(
                    kind="hit_dice",
                    owner_id=pc["entity_id"],
                    pool_id="dice:cleric",
                    current=0,
                    maximum=1,
                    class_slug="cleric",
                    die_size=8,
                ),
                dict(
                    kind="spell_slot",
                    owner_id=pc["entity_id"],
                    pool_id="spell:1",
                    current=0,
                    maximum=2,
                    level=1,
                ),
            ]
        }
    return _validate(data), loader


def rest_command(sm, sid, package, context, key):
    """Create the narrow rest intent tied to the current world version."""
    current = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid, key, current.pc_actor_id, "activity.long_rest", LongRest(), current.world_version
    )


def test_long_rest_commits_time_and_recovery_once_and_persists_cooldown(tmp_path, context):
    package, loader = rest_package()
    host = replace(context, can_authorize_long_rest=True, can_advance_time=True)
    db = tmp_path / "rest.sqlite"
    with StateMachine(db, dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = rest_command(sm, sid, package, host, "rest.first")
        data = asdict(command)
        data.pop("check_world_effects")
        assert world_command_from_data(data) == command
        data["payload"]["duration"] = 1
        with pytest.raises(StateMachineError):
            world_command_from_data(data)
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, package, context=context)
        assert denied.value.code == "unauthorized"
        rng = sm.read_rng_context(sid, package, context=host)
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(command, package, context=host) == receipt
        with pytest.raises(StateMachineError) as replay_denied:
            sm.submit_command(command, package, context=context)
        assert replay_denied.value.code == "unauthorized"
        after = sm.read_noncombat_snapshot(sid, package, context=host)
        old = {actor.entity_id: actor for actor in before.character_states}
        current = {actor.entity_id: actor for actor in after.character_states}
        old_pc = old.pop(command.actor_id)
        pc = current.pop(command.actor_id)
        assert old_pc.hp_current == 2
        assert old == current
        assert all(pool.current == 0 for pool in before.resource_state.pools)
        assert pc.hp_current == pc.hp_max
        assert pc.spell_slots == {1: 2}
        assert {p.pool_id: p.current for p in after.resource_state.pools} == {
            "dice:cleric": 1,
            "spell:1": 2,
        }
        assert sm.read_rng_context(sid, package, context=host) == rng
        assert sm.read_time_sync(sid, package, context=host).committed_tick == 28800
        assert [
            event.event_type
            for event in sm.read_events(sid, package, context=host)
            if event.event_seq in receipt.event_seqs
        ] == ["game.time_advanced", "activity.long_rest", "activity.long_rest"]
        sm.save_session(sid, package, context=host)

    with StateMachine(db, dnd_loader=loader) as sm:
        sm.resume_session(sid, package, context=host)
        assert sm.submit_command(command, package, context=host) == receipt
        assert sm.read_noncombat_snapshot(sid, package, context=host) == after
        early = rest_command(sm, sid, package, host, "rest.early")
        assert sm.submit_command(early, package, context=host).error_code == "rest_cooldown"
        advance = replace(
            early,
            command_id="rest.wait_cooldown",
            command_type="world.advance_time",
            payload=AdvanceGameTime(28800, 28800 + 57600),
        )
        assert sm.submit_command(advance, package, context=host).status == "committed"
        next_rest = rest_command(sm, sid, package, host, "rest.next")
        assert sm.submit_command(next_rest, package, context=host).status == "committed"
        assert sm.read_time_sync(sid, package, context=host).committed_tick == 115200


@pytest.mark.parametrize(
    ("timed", "hp", "error"),
    [(False, 2, "unsupported_time_protocol"), (True, 0, "actor_invalid")],
)
def test_long_rest_without_time_or_hp_has_no_benefit(tmp_path, context, timed, hp, error):
    package, loader = rest_package(timed=timed, hp=hp)
    host = replace(context, can_authorize_long_rest=True)
    with StateMachine(tmp_path / "rest.sqlite", dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = rest_command(sm, sid, package, host, "rest.blocked")
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        refused = sm.submit_command(command, package, context=host)
        assert refused.status == "rejected"
        assert refused.error_code == error
        assert sm.read_noncombat_snapshot(sid, package, context=host) == before
        assert sm.world_time.read(sid, package, context=host).game_tick == 0


@pytest.mark.parametrize("status", ["waiting", "interrupted", "continue"])
def test_long_rest_rolls_back_time_boundary_and_recovery(tmp_path, context, status):
    package, loader = rest_package()
    host = replace(context, can_authorize_long_rest=True)
    with StateMachine(tmp_path / "rest.sqlite", dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = rest_command(sm, sid, package, host, "rest.interrupted")
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        events = sm.read_events(sid, package, context=host)

        class Boundary:
            def next_boundary(self, sid, package):
                if sm._world_facts.time_state(sid)[1] < 60:
                    return 60
                return None

            def settle_one(self, row, package, command_id):
                sm._characters.set_hp(row["session_id"], command.actor_id, 1)
                return DueResult(status=status)

        sm._session_runtime.time.consumers["rest_test"] = Boundary()
        refused = sm.submit_command(command, package, context=host)
        assert refused.status == "rejected", refused
        assert refused.error_code == (
            "rest_interrupted" if status == "continue" else "rest_time_unsettled"
        )
        assert sm.submit_command(command, package, context=host) == refused
        assert sm.read_noncombat_snapshot(sid, package, context=host) == before
        assert sm.read_time_sync(sid, package, context=host).committed_tick == 0
        assert sm.read_events(sid, package, context=host) == events
        assert sm._activity_records.last_long_rest(sid, command.actor_id) is None


def test_late_failure_rolls_back_every_rest_write_and_same_key_can_retry(
    tmp_path, context, monkeypatch
):
    package, loader = rest_package()
    host = replace(context, can_authorize_long_rest=True)
    db = tmp_path / "rest.sqlite"
    with StateMachine(db, dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = rest_command(sm, sid, package, host, "rest.retry")
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        events = sm.read_events(sid, package, context=host)
        finish = sm._session_runtime.finish

        def fail_after_receipt(*args, **kwargs):
            finish(*args, **kwargs)
            raise RuntimeError("after receipt write")

        with monkeypatch.context() as patch:
            patch.setattr(sm._session_runtime, "finish", fail_after_receipt)
            with pytest.raises(RuntimeError, match="after receipt write"):
                sm.submit_command(command, package, context=host)
        assert sm.read_noncombat_snapshot(sid, package, context=host) == before
        assert sm.read_time_sync(sid, package, context=host).committed_tick == 0
        assert sm.read_events(sid, package, context=host) == events
        assert sm._activity_records.last_long_rest(sid, command.actor_id) is None
        assert sm.get_command_receipt(sid, command.command_id, package, context=host) is None

    with StateMachine(db, dnd_loader=loader) as sm:
        assert sm.read_noncombat_snapshot(sid, package, context=host) == before
        assert sm.read_time_sync(sid, package, context=host).committed_tick == 0
        assert sm.submit_command(command, package, context=host).status == "committed"
