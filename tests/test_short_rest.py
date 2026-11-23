"""Public short rest with explicitly chosen dice and the pinned evaluator."""

import json
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import create
from item_support import NPC
from test_cure_wounds import cure_world
from test_long_rest import rest_data, rest_package

from trpg_state_machine import (
    HitDiceSpend,
    ShortRest,
    StateMachine,
    StateMachineError,
    TypedCommand,
    world_command_from_data,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.time_coordination import DueResult

DICE = "dice:fighter"
OTHER_DICE = "dice:warlock"
PACT = "pact:1"
WIND = "feature:second-wind"


def short_rest_package(*, timed=True, hp=2):
    """Author multiple owned Hit Dice plus Pact Magic and Second Wind balances."""
    data, base = rest_data(timed=timed, hp=hp)
    canonical = BundledAssetLoader()
    loader = MemoryAssetLoader(
        classes=[canonical.get_class(s) for s in ("fighter", "warlock")],
        features=[canonical.get_feature("second-wind")],
        items=list(base._items.values()),
        monsters=list(base._monsters.values()),
    )
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    data["pc_templates"][0]["character"]["hp"]["max"] = 30
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        pc = snapshot["character_states"][0]
        pc.update(
            class_slug="fighter",
            classes={"fighter": 2, "warlock": 1},
            character_level=3,
            granted_features=["second-wind"],
            hp_max=30,
            spell_slots={},
            pact_slots={1: 0},
            spellcasting_ability=None,
            custom_counters={"feature_use:second-wind": {"spent": 2}},
        )
        owner = pc["entity_id"]
        snapshot["resource_state"]["pools"] = [
            dict(
                kind="hit_dice",
                owner_id=owner,
                pool_id=DICE,
                current=2,
                maximum=2,
                class_slug="fighter",
                die_size=10,
            ),
            dict(
                kind="hit_dice",
                owner_id=owner,
                pool_id=OTHER_DICE,
                current=1,
                maximum=1,
                class_slug="warlock",
                die_size=8,
            ),
            dict(kind="pact_slot", owner_id=owner, pool_id=PACT, current=0, maximum=1, level=1),
            dict(
                kind="feature_uses",
                owner_id=owner,
                pool_id=WIND,
                current=0,
                maximum=2,
                feature_slug="second-wind",
            ),
        ]
    return _validate(data), loader


def short_command(sm, sid, package, context, key, *spends):
    current = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        current.pc_actor_id,
        "activity.short_rest",
        ShortRest(spends),
        current.world_version,
    )


def short_state(sm, sid, package, context):
    return (
        sm.read_noncombat_snapshot(sid, package, context=context),
        sm.read_rng_context(sid, package, context=context),
        sm.read_time_sync(sid, package, context=context).committed_tick,
        sm.read_events(sid, package, context=context),
    )


def test_short_rest_rolls_and_spends_two_chosen_dice_once(tmp_path, context):
    package, loader = short_rest_package()
    host = replace(context, can_authorize_short_rest=True)
    db = tmp_path / "short.sqlite"
    with StateMachine(db, dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = short_command(sm, sid, package, host, "rest.short", HitDiceSpend(DICE, 2))
        data = asdict(command)
        data.pop("check_world_effects")
        assert world_command_from_data(json.loads(json.dumps(data))) == command
        for invalid in (
            {"hit_dice": [{"pool_id": DICE, "amount": True}]},
            {"hit_dice": [{"pool_id": DICE, "amount": 1}] * 2},
            {"hit_dice": [{"pool_id": DICE, "amount": 1, "healing": 12}]},
            {"hit_dice": [], "duration": 1},
            {"hit_dice": [], "can_authorize_short_rest": True},
        ):
            with pytest.raises(StateMachineError):
                world_command_from_data({**data, "payload": invalid})

        before = short_state(sm, sid, package, host)
        for candidate, trusted in (
            (command, replace(context, can_authorize_long_rest=True, can_advance_time=True)),
            (replace(command, actor_id=NPC), host),
        ):
            with pytest.raises(StateMachineError) as denied:
                sm.submit_command(candidate, package, context=trusted)
            assert denied.value.code == "unauthorized"
        assert short_state(sm, sid, package, host) == before
        assert sm.get_command_receipt(sid, command.command_id, package, context=host) is None
        assert sm.get_command_rolls(sid, command.command_id, package, context=host) is None

        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "committed", receipt
        after = short_state(sm, sid, package, host)
        assert sm.submit_command(command, package, context=host) == receipt
        assert short_state(sm, sid, package, host) == after
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, package, context=context)
        assert denied.value.code == "unauthorized"
        with pytest.raises(StateMachineError) as conflict:
            sm.submit_command(replace(command, payload=ShortRest()), package, context=host)
        assert conflict.value.code == "idempotency_conflict"

        snapshot, rng, tick, events = after
        previous, old_rng, _, _ = before
        old_pc = next(a for a in previous.character_states if a.entity_id == command.actor_id)
        pc = next(a for a in snapshot.character_states if a.entity_id == command.actor_id)
        assert old_pc.hp_current == 2
        rng_copy = old_rng.state.restore()
        con_modifier = (old_pc.constitution - 10) // 2
        rolled = [max(1, rng_copy.randint(1, 10) + con_modifier) for _ in range(2)]
        assert pc.hp_current == min(pc.hp_max, old_pc.hp_current + sum(rolled))
        assert rng.state == RNGState.capture(rng_copy)
        assert rng.version == old_rng.version + 1
        assert tick == 3600
        assert {p.pool_id: p.current for p in snapshot.resource_state.pools} == {
            DICE: 0,
            OTHER_DICE: 1,
            PACT: 1,
            WIND: 1,
        }
        assert pc.pact_slots == {1: 1}
        assert pc.custom_counters["feature_use:second-wind"] == {"spent": 1}
        assert {p.pool_id: p.current for p in previous.resource_state.pools} == {
            DICE: 2,
            OTHER_DICE: 1,
            PACT: 0,
            WIND: 0,
        }
        committed = [e for e in events if e.event_seq in receipt.event_seqs]
        assert [e.event_type for e in committed] == [
            "game.time_advanced",
            "activity.short_rest",
            "activity.short_rest",
        ]
        payload = committed[1].payload
        assert (payload["started_tick"], payload["completed_tick"]) == (0, 3600)
        evidence = payload["engine_event"]
        assert evidence["hit_dice"] == [
            {"pool_id": DICE, "amount": 2, "die_size": 10, "healing_rolls": rolled}
        ]
        assert evidence["hp_regained"] == pc.hp_current - old_pc.hp_current
        projected = sm.get_command_rolls(sid, command.command_id, package, context=host)
        assert projected["rolls"] == [
            {
                "event_seq": committed[1].event_seq,
                "type": "hit_dice_healing",
                "actor_id": command.actor_id,
                "hit_dice": evidence["hit_dice"],
                "hp_regained": pc.hp_current - old_pc.hp_current,
            }
        ]
        assert evidence["pact_slots_restored"] == {"1": 1}
        assert evidence["feature_uses_restored"] == {"second-wind": 1}
        assert sm._activity_records.last_long_rest(sid, command.actor_id) is None

    with StateMachine(db, dnd_loader=loader) as sm:
        sm.resume_session(sid, package, context=host)
        assert sm.submit_command(command, package, context=host) == receipt
        assert short_state(sm, sid, package, host) == after
        assert sm.get_command_rolls(sid, command.command_id, package, context=host) == projected


def test_short_rest_without_dice_recovers_features_without_healing_or_cooldown(tmp_path, context):
    package, loader = short_rest_package()
    host = replace(context, can_authorize_short_rest=True)
    with StateMachine(tmp_path / "no-dice.sqlite", dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        rng = sm.read_rng_context(sid, package, context=host)
        for index in (1, 2):
            command = short_command(sm, sid, package, host, f"rest.no-dice-{index}")
            receipt = sm.submit_command(command, package, context=host)
            assert receipt.status == "committed", receipt
            snapshot, current_rng, tick, _ = short_state(sm, sid, package, host)
            pc = next(a for a in snapshot.character_states if a.entity_id == command.actor_id)
            assert pc.hp_current == 2
            assert current_rng == rng
            assert tick == index * 3600
            assert {p.pool_id: p.current for p in snapshot.resource_state.pools} == {
                DICE: 2,
                OTHER_DICE: 1,
                PACT: 1,
                WIND: index,
            }


def test_short_rest_projection_keeps_two_pool_order_and_one_hp_total(tmp_path, context):
    """A rest's aggregate HP belongs to the ordered set of spent pools once."""
    package, loader = short_rest_package()
    host = replace(context, can_authorize_short_rest=True)
    with StateMachine(tmp_path / "two-pools.sqlite", dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = short_command(
            sm,
            sid,
            package,
            host,
            "rest.two-pools",
            HitDiceSpend(DICE, 1),
            HitDiceSpend(OTHER_DICE, 1),
        )
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "committed"
        (roll,) = sm.get_command_rolls(sid, command.command_id, package, context=host)["rolls"]
        assert roll["type"] == "hit_dice_healing"
        assert [spend["pool_id"] for spend in roll["hit_dice"]] == [DICE, OTHER_DICE]
        assert [spend["die_size"] for spend in roll["hit_dice"]] == [10, 8]
        assert all(len(spend["healing_rolls"]) == 1 for spend in roll["hit_dice"])
        assert roll["hp_regained"] <= sum(sum(spend["healing_rolls"]) for spend in roll["hit_dice"])


def test_short_rest_keeps_ordinary_spell_slots_unchanged(tmp_path, context):
    package, loader = rest_package()
    host = replace(context, can_authorize_short_rest=True)
    with StateMachine(tmp_path / "ordinary-slot.sqlite", dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = short_command(sm, sid, package, host, "rest.cleric")
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=host)
        assert after.character_states == before.character_states
        assert after.resource_state == before.resource_state
        assert sm.read_time_sync(sid, package, context=host).committed_tick == 3600


@pytest.mark.parametrize(
    ("timed", "hp", "spend", "error"),
    [
        (False, 2, HitDiceSpend(DICE, 1), "unsupported_time_protocol"),
        (True, 0, HitDiceSpend(DICE, 1), "actor_invalid"),
        (True, 2, HitDiceSpend(DICE, 3), "rest.insufficient"),
        (True, 2, HitDiceSpend(PACT, 1), "rest.insufficient"),
        (True, 2, HitDiceSpend("dice:unknown", 1), "rest.insufficient"),
    ],
)
def test_short_rest_refuses_invalid_inputs_without_consumption(
    tmp_path, context, timed, hp, spend, error
):
    package, loader = short_rest_package(timed=timed, hp=hp)
    host = replace(context, can_authorize_short_rest=True)
    with StateMachine(tmp_path / "invalid.sqlite", dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = short_command(sm, sid, package, host, "rest.invalid", spend)
        before = short_state(sm, sid, package, host)
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "rejected"
        assert receipt.error_code == error
        assert short_state(sm, sid, package, host) == before


def test_short_rest_refuses_active_combat_before_creating_candidate(tmp_path, context, monkeypatch):
    host = replace(context, can_authorize_short_rest=True)
    sm, sid, package, _, _ = cure_world(tmp_path, host, timed=True)
    with sm:

        def forbidden(*args, **kwargs):
            pytest.fail("active combat must refuse short rest before rule evaluation")

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", forbidden)
        command = short_command(sm, sid, package, host, "rest.combat", HitDiceSpend(DICE, 1))
        before = sm.read_combat_snapshot(sid, package, context=host)
        rng = sm.read_rng_context(sid, package, context=host)
        tick = sm.read_time_sync(sid, package, context=host).committed_tick
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "rejected"
        assert receipt.error_code == "unsupported_combat_world_operation"
        assert sm.read_combat_snapshot(sid, package, context=host) == before
        assert sm.read_rng_context(sid, package, context=host) == rng
        assert sm.read_time_sync(sid, package, context=host).committed_tick == tick


@pytest.mark.parametrize(
    ("boundary_status", "error"),
    [
        ("waiting", "rest_time_unsettled"),
        ("continue", "rest_interrupted"),
    ],
)
def test_short_rest_rolls_back_time_boundary(tmp_path, context, boundary_status, error):
    package, loader = short_rest_package()
    host = replace(context, can_authorize_short_rest=True)
    with StateMachine(tmp_path / "boundary.sqlite", dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = short_command(sm, sid, package, host, "rest.boundary", HitDiceSpend(DICE, 2))
        before = short_state(sm, sid, package, host)

        class Boundary:
            def next_boundary(self, sid, package):
                return 60 if sm._world_facts.time_state(sid)[1] < 60 else None

            def settle_one(self, row, package, command_id):
                sm._characters.set_hp(row["session_id"], command.actor_id, 1)
                return DueResult(status=boundary_status)

        sm._session_runtime.time.consumers["rest_test"] = Boundary()
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "rejected"
        assert receipt.error_code == error
        assert sm.submit_command(command, package, context=host) == receipt
        assert short_state(sm, sid, package, host) == before


def test_short_rest_retries_after_fault_following_receipt_write(tmp_path, context, monkeypatch):
    package, loader = short_rest_package()
    host = replace(context, can_authorize_short_rest=True)
    db = tmp_path / "late-failure.sqlite"
    with StateMachine(db, dnd_loader=loader) as sm:
        sid = create(sm, package, host)
        command = short_command(sm, sid, package, host, "rest.retry", HitDiceSpend(DICE, 2))
        before = short_state(sm, sid, package, host)
        finish = sm._session_runtime.finish

        def fail_after_receipt(*args, **kwargs):
            finish(*args, **kwargs)
            assert sm._world_facts.time_state(sid)[1] == 3600
            assert sm._session_runtime._rng_context(sid) != before[1]
            db = sm._store.connection
            assert (
                db.execute(
                    "SELECT current_hp FROM characters WHERE session_id=? AND actor_id=?",
                    (sid, command.actor_id),
                ).fetchone()[0]
                > 2
            )
            assert dict(
                db.execute(
                    "SELECT resource_id,current FROM character_resources "
                    "WHERE session_id=? AND actor_id=?",
                    (sid, command.actor_id),
                )
            ) == {DICE: 0, OTHER_DICE: 1, PACT: 1, WIND: 1}
            raise RuntimeError("after receipt write")

        with monkeypatch.context() as patch:
            patch.setattr(sm._session_runtime, "finish", fail_after_receipt)
            with pytest.raises(RuntimeError, match="after receipt write"):
                sm.submit_command(command, package, context=host)
        assert short_state(sm, sid, package, host) == before
        assert sm.get_command_receipt(sid, command.command_id, package, context=host) is None

    with StateMachine(db, dnd_loader=loader) as sm:
        assert short_state(sm, sid, package, host) == before
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "committed", receipt
        after = short_state(sm, sid, package, host)
        assert sm.submit_command(command, package, context=host) == receipt
        assert short_state(sm, sid, package, host) == after
        assert after[1].version == before[1].version + 1
        assert after[2] == 3600
