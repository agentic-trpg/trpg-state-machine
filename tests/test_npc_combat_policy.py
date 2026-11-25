"""A selected enemy goal controls a bounded real combat Turn."""

from dataclasses import replace
from hashlib import sha256
from random import Random

import pytest
import yaml
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.monster_rules import monster_rule_facts
from dnd_support import NPC, WEAPON, create, initialize, loader, pass_turn
from dnd_support import package_data as real_data
from modern_support import modern_loader
from test_combat_closure import close_command, pc_attack
from test_combat_start import opening_data, start_command
from test_dnd_runtime import attack
from test_npc_evaluation_tasks import create_task
from test_npc_state import SOURCE

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.npc_results import NPCEvaluationResult
from trpg_state_machine.npc_state import Goal, SourceReference
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import NPCContext


def _ready_enemy(tmp_path, context, mode, *, distant=False, unbound=False, unseen=False):
    """Create one live enemy Turn and adopt a sourced, exact policy goal."""
    assets = loader()
    data = real_data(yaml.safe_load(SOURCE.read_text("utf-8")), assets)
    if unseen:
        data["actors"][0]["senses"]["visual"] = False
    if distant:
        data["rule_bindings"]["combat_foundation"]["snapshot"]["combat_state"]["actor_zone"][
            NPC
        ] = "2,0"
    if unbound:
        foundation = data["rule_bindings"]["combat_foundation"]["snapshot"]
        foundation["combat_state"]["monster_slug_by_entity"].pop(NPC)
        actor = foundation["character_states"][1]
        actor.update(carried_item_slugs=[WEAPON], weapon_in_hands=WEAPON, weapon_grip="one_handed")
        foundation["inventory_state"]["entries"].append(
            {
                "instance_id": "inventory.npc.lantern",
                "owner_id": NPC,
                "item_slug": WEAPON,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )
    package = _validate(data)
    sm = StateMachine(tmp_path / "policy.db", dnd_loader=assets)
    sid = create(sm, package, context)
    initialize(sm, sid, package, context)
    sm.submit_command(attack(sm, sid, package, context), package, context=context)
    pass_turn(sm, sid, package, context)
    _adopt_goal(sm, sid, package, context, mode)
    return sm, sid, package


def _adopt_goal(sm, sid, package, context, mode):
    """Commit the policy as a sourced NPC evaluation result."""
    sm.grant_npc_access(sid, NPC, "agent:caretaker", package, context=context)
    npc = NPCContext("agent:caretaker", sid, NPC)
    task = create_task(sm, sid, package, npc)
    goal = Goal(
        record_id="goal.combat.policy",
        version=1,
        is_current=True,
        text=mode,
        sources=(SourceReference("package", NPC, None),),
        status="active",
    )
    receipt = sm.submit_npc_evaluation_result(
        task.evaluation_id, NPCEvaluationResult(goal_updates=(goal,)), package, context=npc
    )
    assert receipt.status == "completed"


@pytest.mark.parametrize("mode,expected", [("engage", ("attack", "pass")), ("hold", ("pass",))])
def test_adopted_macro_changes_enemy_turn(tmp_path, context, mode, expected):
    sm, sid, package = _ready_enemy(tmp_path, context, mode)
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context).combat_state
        request = dict(
            context=context,
            attack_id="scimitar",
            expected_combat_id=before.combat_id,
            expected_turn_serial=before.turn_serial,
        )
        result = sm.run_enemy_turn(sid, NPC, package, **request)
        assert result.status == "completed"
        assert tuple(r.status for r in result.receipts) == ("committed",) * len(expected)
        assert tuple(r.command_id.rsplit(":", 1)[-1] for r in result.receipts) == expected
        assert (
            sm.read_combat_snapshot(sid, package, context=context).combat_state.initiative_ids[
                sm.read_combat_snapshot(
                    sid, package, context=context
                ).combat_state.current_turn_index
            ]
            != NPC
        )
        assert sm.run_enemy_turn(sid, NPC, package, **request) == result


def test_enemy_moves_into_melee_then_attacks_and_passes(tmp_path, context):
    sm, sid, package = _ready_enemy(tmp_path, context, "engage", distant=True, unbound=True)
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context).combat_state
        result = sm.run_enemy_turn(
            sid,
            NPC,
            package,
            context=context,
            attack_id=WEAPON,
            expected_combat_id=before.combat_id,
            expected_turn_serial=before.turn_serial,
        )
        assert result.status == "completed", result
        assert tuple(r.command_id.rsplit(":", 1)[-1] for r in result.receipts) == (
            "move",
            "attack",
            "pass",
        ), result
        assert all(r.status == "committed" for r in result.receipts)
        assert (
            sm.read_combat_snapshot(sid, package, context=context).combat_state.actor_zone[NPC]
            == "1,0"
        )


def test_canonical_bandit_moves_attacks_and_passes_in_dynamic_encounter(tmp_path, context):
    """Complete an exploration/combat cycle with a canonical mobile enemy."""
    assets = modern_loader()
    data = opening_data(yaml.safe_load(SOURCE.read_text("utf-8")), assets)
    data.pop("time_binding")  # NPC evaluation and grants require the legacy clock.
    data["actors"][0]["initial_cell"] = "2,0"
    foundation = data["rule_bindings"]["noncombat_foundation"]
    foundation["rng_state"] = RNGState.capture(Random(9)).model_dump(mode="json")
    foundation["snapshot"]["character_states"][0]["hp_current"] = 8
    data["pc_templates"][0]["character"]["hp"]["current"] = 8
    bandit = assets.get_monster("bandit")
    foundation["snapshot"]["character_states"][1].update(
        {
            name: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            for name, value in monster_rule_facts(bandit).items()
        },
        dexterity=bandit.ability_scores.dex,
        hp_current=1,
        hp_max=bandit.hp,
        ac=bandit.ac,
        attack_bonus=None,
        creature_type=bandit.creature_type,
        base_speed=bandit.movement.walk,
        movement_remaining=bandit.movement.walk,
        senses=bandit.senses.model_dump(mode="json", exclude={"passive_perception"}),
        damage_resistances=list(bandit.damage_resistances),
        damage_immunities=list(bandit.damage_immunities),
        damage_vulnerabilities=list(bandit.damage_vulnerabilities),
        condition_immunities=list(bandit.condition_immunities),
        physical_resistances_nonmagical_only=False,
        granted_features=[],
    )
    package = _validate(data)
    host = replace(context, can_start_combat=True)

    with StateMachine(tmp_path / "bandit.db", dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        initial = sm.read_session(sid, package, context=host)
        assert initial.phase == "exploration"
        pc = initial.pc_actor_id
        _adopt_goal(sm, sid, package, host, "engage")
        opening = start_command(sm, sid, package, host, initiative_order=(pc, NPC))
        opened = sm.submit_command(opening, package, context=host)
        assert opened.status == "committed", opened
        assert pass_turn(sm, sid, package, host).actor_id == pc
        before = sm.read_combat_snapshot(sid, package, context=host)
        combat = before.combat_state
        assert combat.initiative_ids[combat.current_turn_index] == NPC
        assert combat.monster_slug_by_entity == {NPC: "bandit"}
        assert combat.actor_zone == {pc: "0,0", NPC: "2,0"}
        before_rng = sm.read_rng_context(sid, package, context=host)
        rolls = before_rng.state.restore()
        natural = rolls.randint(1, 20)
        expected_damage = rolls.randint(1, 6) + 1
        request = dict(
            context=host,
            attack_id="scimitar",
            expected_combat_id=combat.combat_id,
            expected_turn_serial=combat.turn_serial,
        )
        result = sm.run_enemy_turn(sid, NPC, package, **request)
        assert result.status == "completed", result
        assert all(r.status == "committed" and r.actor_id == NPC for r in result.receipts), [
            (r.status, r.error_code) for r in result.receipts
        ]
        assert tuple(r.command_id.rsplit(":", 1)[-1] for r in result.receipts) == (
            "move",
            "attack",
            "pass",
        ), result
        _, attack_receipt, _ = result.receipts
        events = sm.read_events(sid, package, context=host)
        attack_events = [
            event.payload["engine_event"]
            for event in events
            if event.event_seq in attack_receipt.event_seqs
            and event.event_type == "combat.engine_event"
        ]
        (roll,) = [event for event in attack_events if event["type"] == "attack_rolled"]
        assert (roll["attacker_id"], roll["target_id"], roll["natural"], roll["modifier"]) == (
            NPC,
            pc,
            natural,
            3,
        )
        assert roll["is_hit"] and roll["roll_total"] == natural + 3
        (damage,) = [event for event in attack_events if event["type"] == "damage_applied"]
        assert (damage["target_id"], damage["damage_type"], damage["amount"]) == (
            pc,
            "slashing",
            expected_damage,
        )
        after = sm.read_combat_snapshot(sid, package, context=host)
        current = after.combat_state
        assert current.actor_zone == {pc: "0,0", NPC: "1,0"}
        assert current.movement_ledgers[NPC].spent_ft == 5
        assert current.initiative_ids[current.current_turn_index] == pc
        assert current.turn_serial == combat.turn_serial + 1
        before_hp = next(
            actor.hp_current for actor in before.character_states if actor.entity_id == pc
        )
        after_hp = next(
            actor.hp_current for actor in after.character_states if actor.entity_id == pc
        )
        assert after_hp == before_hp - expected_damage
        rng = sm.read_rng_context(sid, package, context=host)
        assert rng.version == before_rng.version + 1
        assert rng.state == RNGState.capture(rolls)
        assert sm.run_enemy_turn(sid, NPC, package, **request) == result
        assert sm.read_combat_snapshot(sid, package, context=host) == after
        assert sm.read_rng_context(sid, package, context=host) == rng
        assert sm.read_events(sid, package, context=host) == events

        strike = sm.submit_command(pc_attack(sm, sid, package, host), package, context=host)
        assert strike.status == "committed", strike
        assert NPC in sm.read_combat_snapshot(sid, package, context=host).combat_state.dead_ids
        closing = sm.submit_command(close_command(sm, sid, package, host), package, context=host)
        assert closing.status == "committed", closing
        assert sm.read_session(sid, package, context=host).phase == "exploration"


def test_noncanonical_bandit_move_is_rejected_then_passes_same_turn(tmp_path, context):
    sm, sid, package = _ready_enemy(tmp_path, context, "engage", distant=True)
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context).combat_state
        request = dict(
            context=context,
            attack_id="scimitar",
            expected_combat_id=before.combat_id,
            expected_turn_serial=before.turn_serial,
        )
        result = sm.run_enemy_turn(sid, NPC, package, **request)
        assert result.status == "completed", result
        assert tuple(r.command_id.rsplit(":", 1)[-1] for r in result.receipts) == ("move", "pass")
        assert tuple(r.status for r in result.receipts) == ("rejected", "committed")
        assert result.receipts[0].error_code == "movement.template"
        assert sm.run_enemy_turn(sid, NPC, package, **request) == result


def test_unseen_target_cell_does_not_drive_movement(tmp_path, context):
    sm, sid, package = _ready_enemy(tmp_path, context, "engage", distant=True, unseen=True)
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context).combat_state
        result = sm.run_enemy_turn(
            sid,
            NPC,
            package,
            context=context,
            attack_id="scimitar",
            expected_combat_id=before.combat_id,
            expected_turn_serial=before.turn_serial,
        )
        assert result.status == "completed"
        assert tuple(r.command_id.rsplit(":", 1)[-1] for r in result.receipts) == ("pass",)
        assert (
            sm.read_combat_snapshot(sid, package, context=context).combat_state.actor_zone[NPC]
            == "2,0"
        )


def test_lost_pass_reply_replays_entire_turn(tmp_path, context):
    sm, sid, package = _ready_enemy(tmp_path, context, "engage")
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context).combat_state
        request = dict(
            context=context,
            attack_id="scimitar",
            expected_combat_id=before.combat_id,
            expected_turn_serial=before.turn_serial,
        )
        submit = sm.submit_command

        def lose_pass(command, *args, **kwargs):
            """Lose only the reply to a durably committed enemy Pass."""
            receipt = submit(command, *args, **kwargs)
            if command.command_id.endswith(":pass"):
                raise RuntimeError("lost Pass reply")
            return receipt

        sm.submit_command = lose_pass
        with pytest.raises(RuntimeError, match="lost Pass reply"):
            sm.run_enemy_turn(sid, NPC, package, **request)
        sm.submit_command = submit
        replay = sm.run_enemy_turn(sid, NPC, package, **request)
        assert replay.status == "completed"
        assert tuple(r.command_id.rsplit(":", 1)[-1] for r in replay.receipts) == ("attack", "pass")
        assert all(r.status == "committed" for r in replay.receipts)


def test_policy_version_rechecked_at_command_submission(tmp_path, context):
    sm, sid, package = _ready_enemy(tmp_path, context, "engage")
    with sm:
        npc = NPCContext("agent:caretaker", sid, NPC)
        before = sm.read_combat_snapshot(sid, package, context=context)
        old_version = sm.get_npc_view(sid, NPC, package, context=npc).npc_state_version
        task = create_task(
            sm,
            sid,
            package,
            npc,
            key="task.policy.change",
            expected_npc_state_version=old_version,
        )
        changed = Goal(
            record_id="goal.combat.policy",
            version=2,
            is_current=True,
            text="hold",
            sources=(SourceReference("package", NPC, None),),
            status="active",
        )
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id,
                NPCEvaluationResult(goal_updates=(changed,)),
                package,
                context=npc,
            ).status
            == "completed"
        )
        key = (
            f"npc-policy:{before.combat_state.combat_id}:{before.combat_state.turn_serial}:"
            f"{old_version}:engage:{sha256(b'scimitar').hexdigest()[:16]}:pass"
        )
        command = TypedCommand(
            sid,
            key,
            NPC,
            "combat.intent",
            CombatIntentPayload(intent_type="pass", source_id=NPC),
            before.world_version,
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "policy_changed"
        assert sm.get_command_receipt(sid, key, package, context=context) is None
