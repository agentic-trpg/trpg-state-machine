"""A selected enemy goal controls a bounded real combat Turn."""

from hashlib import sha256

import pytest
import yaml
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import NPC, WEAPON, create, initialize, loader, pass_turn
from dnd_support import package_data as real_data
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
    sm.grant_npc_access(sid, NPC, "agent:caretaker", package, context=context)
    npc = NPCContext("agent:caretaker", sid, NPC)
    sm.submit_command(attack(sm, sid, package, context), package, context=context)
    pass_turn(sm, sid, package, context)
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
    return sm, sid, package


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


def test_unsupported_move_returns_receipt_and_passes_same_turn(tmp_path, context):
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
