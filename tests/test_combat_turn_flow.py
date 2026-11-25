"""Real commands for weapon action, movement and explicit turn handoffs."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd_support import NPC, create, initialize, pass_turn
from modern_support import modern_data, modern_loader
from test_combat_closure import pc_attack
from test_combat_start import start_command
from test_hold_effect_candidate import pass_command
from test_timed_spell_cast import move_command

from trpg_state_machine import (
    CombatWithdraw,
    StateMachine,
    StepActor,
    TimeBinding,
    TypedCommand,
)
from trpg_state_machine.package import _validate

ATTENDANT = "npc.combatattendant"


@pytest.mark.parametrize("npc_hp", [40, 1], ids=["living-next-actor", "skip-dead-actor"])
@pytest.mark.parametrize("dynamic", [False, True], ids=["authored", "dynamic"])
def test_move_attack_move_then_end_turn(tmp_path, package_data, context, npc_hp, dynamic):
    """Weapon spend holds the turn and explicit Pass follows the fixed initiative."""
    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=npc_hp, spatial=True)
    data["time_binding"] = asdict(TimeBinding())
    for actor in data["actors"]:
        actor.pop("npc_state", None)
    first = next(actor for actor in data["actors"] if actor["actor_id"] == NPC)
    attendant = {
        **deepcopy(first),
        "actor_id": ATTENDANT,
        "name": "Attendant",
        "initial_cell": "2,0",
    }
    data["actors"].append(attendant)
    data["scenes"][0]["actor_ids"].append(ATTENDANT)
    for key, foundation in data["rule_bindings"].items():
        snapshot = foundation["snapshot"]
        npc = snapshot["character_states"][1]
        snapshot["character_states"].append(
            {**deepcopy(npc), "entity_id": ATTENDANT, "name": "Attendant", "initiative": 0}
        )
        if key == "noncombat_foundation":
            foundation["xp"][ATTENDANT] = 0
            continue
        combat = snapshot["combat_state"]
        combat["initiative_ids"].append(ATTENDANT)
        combat["encounter_ids"].append(ATTENDANT)
        combat["actor_zone"][ATTENDANT] = attendant["initial_cell"]
        # These NPCs use their live mechanical facts, with no monster reach profile.
        combat["monster_slug_by_entity"] = {}
        for field in ("movement_ledgers", "xp_value_by_entity"):
            combat[field][ATTENDANT] = deepcopy(combat[field][NPC])
    if dynamic:
        data["rule_bindings"].pop("combat_foundation")
    package = _validate(data)
    context = replace(
        context, controlled_npc_ids=(*context.controlled_npc_ids, ATTENDANT), can_start_combat=True
    )

    with StateMachine(tmp_path / "turn-flow.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        player = sm.read_session(sid, package, context=context).pc_actor_id
        if dynamic:
            roster = (player, NPC, ATTENDANT)
            start = start_command(
                sm,
                sid,
                package,
                context,
                participant_ids=roster,
                initiative_order=roster,
                opening_cell=None,
                area_cells=("0,0", "0,1", "1,0", "1,1", "2,0"),
                opponent_profiles=(),
            )
            receipt = sm.submit_command(start, package, context=context)
            assert receipt.status == "committed", receipt
        else:
            assert initialize(sm, sid, package, context).status == "committed"
        session = (sm, sid, package, context)
        initial = sm.read_combat_snapshot(sid, package, context=context)
        assert initial.combat_state.party_ids == {player}
        first_step = move_command(session, "0,1", "first-step")
        first_step_receipt = sm.submit_command(first_step, package, context=context)
        assert first_step_receipt.status == "committed", first_step_receipt
        attack = pc_attack(sm, sid, package, context)
        attack_receipt = sm.submit_command(attack, package, context=context)
        assert attack_receipt.status == "committed", attack_receipt
        after_attack = sm.read_combat_snapshot(sid, package, context=context)
        state = after_attack.combat_state
        assert state.initiative_ids[state.current_turn_index] == player
        assert state.turn_serial == initial.combat_state.turn_serial
        assert state.round_number == initial.combat_state.round_number
        active = next(actor for actor in after_attack.character_states if actor.entity_id == player)
        assert not active.action_available and active.action_taken_this_turn
        assert active.movement_remaining == 25
        assert (NPC in state.dead_ids) == (npc_hp == 1)
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 0
        spent = pc_attack(sm, sid, package, context, "already-spent")
        assert sm.submit_command(spent, package, context=context).status == "rejected"

        step = move_command(session, "1,1", "second-step")
        second_step_receipt = sm.submit_command(step, package, context=context)
        assert second_step_receipt.status == "committed", second_step_receipt
        before_end = sm.read_combat_snapshot(sid, package, context=context)
        assert before_end.combat_state.actor_zone[player] == "1,1"
        assert before_end.combat_state.movement_ledgers[player].spent_ft == 10
        end = pass_command(sid, before_end, "player-pass")
        passed = sm.submit_command(end, package, context=context)
        assert passed.status == "committed", passed
        assert sm.submit_command(end, package, context=context) == passed
        after_end = sm.read_combat_snapshot(sid, package, context=context)
        state = after_end.combat_state
        assert state.initiative_ids[state.current_turn_index] == (ATTENDANT if npc_hp == 1 else NPC)
        assert state.round_number == initial.combat_state.round_number
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 0
        attempt = pc_attack(sm, sid, package, context, "outside-own-turn")
        assert sm.submit_command(attempt, package, context=context).error_code == "not_actor_turn"
        bypass = TypedCommand(
            sid,
            "exploration-in-combat",
            player,
            "world.step_actor",
            StepActor(after_end.scene_state.scene_id, "1,1", "0,1"),
            after_end.world_version,
        )
        bypass_receipt = sm.submit_command(bypass, package, context=context)
        assert (bypass_receipt.status, bypass_receipt.error_code) == (
            "rejected",
            "unsupported_combat_world_operation",
        )

        if npc_hp != 1:
            pass_turn(sm, sid, package, context, "first-npc-pass")
            state = sm.read_combat_snapshot(sid, package, context=context).combat_state
            assert state.initiative_ids[state.current_turn_index] == ATTENDANT
            assert sm.read_time_sync(sid, package, context=context).committed_tick == 0
        pass_turn(sm, sid, package, context, "attendant-pass")
        next_round = sm.read_combat_snapshot(sid, package, context=context)
        state = next_round.combat_state
        assert state.initiative_ids[state.current_turn_index] == player
        assert state.round_number == initial.combat_state.round_number + 1
        assert state.movement_ledgers[player].spent_ft == 0
        assert next(
            actor for actor in next_round.character_states if actor.entity_id == player
        ).action_available
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 6
        if dynamic and npc_hp != 1:
            outside = move_command(session, "2,1", "move-across-boundary")
            refused = sm.submit_command(outside, package, context=context)
            assert refused.status == "rejected", refused
            assert sm.read_combat_snapshot(sid, package, context=context) == next_round
            withdraw = TypedCommand(
                sid,
                "withdraw",
                player,
                "combat.withdraw",
                CombatWithdraw(next_round.scene_state.scene_id, "1,1", target_cell="2,1"),
                next_round.world_version,
            )
            withdrew = sm.submit_command(withdraw, package, context=context)
            assert withdrew.status == "committed", withdrew
            assert sm.submit_command(withdraw, package, context=context) == withdrew
            assert sm.read_session(sid, package, context=context).phase == "exploration"
            assert sm.read_actor_position(sid, player, package, context=context).cell == "2,1"
            assert sm.read_time_sync(sid, package, context=context).committed_tick == 6
            explore = TypedCommand(
                sid,
                "explore-after-withdrawal",
                player,
                "world.step_actor",
                StepActor(next_round.scene_state.scene_id, "2,1", "1,1"),
                withdrew.world_version,
            )
            explored = sm.submit_command(explore, package, context=context)
            assert explored.status == "committed", explored
            again = start_command(
                sm,
                sid,
                package,
                context,
                "second-encounter",
                participant_ids=roster,
                initiative_order=roster,
                opening_cell="1,1",
                opponent_profiles=(),
            )
            restarted = sm.submit_command(again, package, context=context)
            assert restarted.status == "committed", restarted
            second = sm.read_combat_snapshot(sid, package, context=context).combat_state
            assert second.combat_id != state.combat_id
            assert second.round_number == 1 and second.actor_zone[player] == "1,1"
