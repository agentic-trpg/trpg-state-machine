"""Public defensive Action admission, payment and retained-turn receipts."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import NPC, create
from test_combat_withdrawal import (
    command,
    command_events,
    start_encounter,
    walk,
    withdrawal_package,
)

from trpg_state_machine import CombatOpponent, CombatOpportunity, StateMachine, StateMachineError


@pytest.mark.parametrize("kind", ["dash", "dodge", "disengage"])
def test_public_defensive_action_pays_without_advancing_turn(tmp_path, package_data, context, kind):
    package, assets = withdrawal_package(package_data)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / f"{kind}.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        action = command(
            sm, sid, package, context, kind, "combat.intent", CombatIntentPayload(intent_type=kind)
        )
        receipt = sm.submit_command(action, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        actor = next(a for a in after.character_states if a.entity_id == pc)
        assert not actor.action_available and actor.action_taken_this_turn
        assert actor.dodging == (kind == "dodge")
        assert actor.disengaging_this_turn == (kind == "disengage")
        assert after.combat_state.current_turn_index == before.combat_state.current_turn_index
        assert after.combat_state.turn_serial == before.combat_state.turn_serial
        assert after.combat_state.movement_ledgers[pc].dash_count == int(kind == "dash")
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.submit_command(action, package, context=context) == receipt


@pytest.mark.parametrize("accept", [True, False], ids=["accept", "decline"])
def test_pc_predeclared_opportunity_on_npc_walk(tmp_path, package_data, context, accept):
    package, assets = withdrawal_package(package_data, npc_at_reach=True, reviewed_reactions=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "opportunity.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context, npc_first=True, distance=20)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        assert before.combat_state.initiative_ids[before.combat_state.current_turn_index] == NPC
        assert NPC in before.combat_state.encounter_ids
        assert next(a for a in before.character_states if a.entity_id == pc).reaction_available
        assert not before.combat_state.ended
        assert not before.combat_state.pending_reactions
        assert pc in before.combat_state.party_ids
        decision = command(
            sm,
            sid,
            package,
            context,
            "reaction-decision",
            "combat.opportunity",
            CombatOpportunity(NPC, accept),
        )
        chosen = sm.submit_command(decision, package, context=context)
        assert chosen.status == "committed", chosen
        waiting = sm.read_combat_snapshot(sid, package, context=context)
        assert waiting.combat_state.actor_zone == before.combat_state.actor_zone
        assert waiting.combat_state.pending_reactions
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.submit_command(decision, package, context=context) == chosen
        stale = command(
            sm,
            sid,
            package,
            context,
            "stale-walk",
            "combat.intent",
            CombatIntentPayload(intent_type="move", target_zone_id="3,0"),
            actor=NPC,
        )
        stale = replace(stale, expected_world_version=before.world_version)
        conflict = sm.submit_command(stale, package, context=context)
        assert (conflict.status, conflict.error_code) == ("conflict", "version_conflict")
        assert sm.read_combat_snapshot(sid, package, context=context) == waiting
        with pytest.raises(StateMachineError, match="Trusted NPC control grant required"):
            sm.submit_command(
                walk(sm, sid, package, context, "unauthorized-walk", "3,0", actor=NPC),
                package,
                context=replace(context, controlled_npc_ids=()),
            )
        move = walk(sm, sid, package, context, "move-after-choice", "3,0", actor=NPC)
        outcome = sm.submit_command(move, package, context=context)
        assert outcome.status == "committed", outcome
        after = sm.read_combat_snapshot(sid, package, context=context)
        reactor = next(a for a in after.character_states if a.entity_id == pc)
        engine_events = [
            event.payload["engine_event"]
            for event in command_events(sm, sid, package, context, outcome)
            if event.event_type == "combat.engine_event"
        ]
        assert any(event["type"] == "attack_rolled" for event in engine_events) is accept
        assert any(event["type"] == "actor_moved" for event in engine_events)
        assert not after.combat_state.pending_reactions
        assert reactor.reaction_available is (not accept)
        assert after.combat_state.current_turn_index == before.combat_state.current_turn_index
        assert (sm.read_rng_context(sid, package, context=context) != rng) is accept
        assert sm.submit_command(move, package, context=context) == outcome


@pytest.mark.parametrize("accept", [True, False], ids=["accepted", "declined"])
def test_unused_opportunity_expires_on_npc_pass(tmp_path, package_data, context, accept):
    package, assets = withdrawal_package(package_data, npc_at_reach=True, reviewed_reactions=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "unused-opportunity.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        start_encounter(sm, sid, package, context, npc_first=True, distance=20)
        decision = command(
            sm,
            sid,
            package,
            context,
            "unused-reaction",
            "combat.opportunity",
            CombatOpportunity(NPC, accept),
        )
        assert sm.submit_command(decision, package, context=context).status == "committed"
        receipt = sm.submit_command(
            command(
                sm,
                sid,
                package,
                context,
                "npc-pass",
                "combat.intent",
                CombatIntentPayload(intent_type="pass"),
                actor=NPC,
            ),
            package,
            context=context,
        )
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert not after.combat_state.pending_reactions


def test_dodge_changes_following_real_npc_attack(tmp_path, package_data, context):
    package, assets = withdrawal_package(package_data, npc_at_reach=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "dodge-defense.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(
            sm, sid, package, context, distance=20, profiles=(CombatOpponent(NPC, "bandit"),)
        )
        dodge = command(
            sm,
            sid,
            package,
            context,
            "dodge",
            "combat.intent",
            CombatIntentPayload(intent_type="dodge"),
        )
        assert sm.submit_command(dodge, package, context=context).status == "committed"
        passed = command(
            sm,
            sid,
            package,
            context,
            "pc-pass",
            "combat.intent",
            CombatIntentPayload(intent_type="pass"),
        )
        assert sm.submit_command(passed, package, context=context).status == "committed"
        attack = command(
            sm,
            sid,
            package,
            context,
            "ogre-attack",
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack", target_id=pc, stat_block_action_id="scimitar"
            ),
            actor=NPC,
        )
        receipt = sm.submit_command(attack, package, context=context)
        assert receipt.status == "committed", receipt
        rolls = [
            event.payload["engine_event"]
            for event in command_events(sm, sid, package, context, receipt)
            if event.payload.get("engine_event", {}).get("type") == "attack_rolled"
        ]
        assert len(rolls) == 1
        assert rolls[0]["advantage"] == "disadvantage"


def test_dash_pays_for_an_extra_public_walk(tmp_path, package_data, context):
    package, assets = withdrawal_package(package_data, budget=5)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "dash-extra.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context, distance=20)
        too_far = walk(sm, sid, package, context, "before-dash", "0,2")
        refused = sm.submit_command(too_far, package, context=context)
        assert (refused.status, refused.error_code) == ("rejected", "movement.insufficient")
        dash = command(
            sm,
            sid,
            package,
            context,
            "dash-action",
            "combat.intent",
            CombatIntentPayload(intent_type="dash"),
        )
        assert sm.submit_command(dash, package, context=context).status == "committed"
        move = walk(sm, sid, package, context, "after-dash", "0,2")
        assert sm.submit_command(move, package, context=context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state.actor_zone[pc] == "0,2"
        assert after.combat_state.movement_ledgers[pc].spent_ft == 10


def test_disengage_allows_public_walk_out_of_threatened_reach(tmp_path, package_data, context):
    package, assets = withdrawal_package(package_data, npc_at_reach=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "disengage-threat.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context, distance=20)
        refused = sm.submit_command(
            walk(sm, sid, package, context, "threatened-walk", "0,3"),
            package,
            context=context,
        )
        assert (refused.status, refused.error_code) == ("rejected", "movement.opportunity_attack")
        action = command(
            sm,
            sid,
            package,
            context,
            "disengage-action",
            "combat.intent",
            CombatIntentPayload(intent_type="disengage"),
        )
        assert sm.submit_command(action, package, context=context).status == "committed"
        walked = sm.submit_command(
            walk(sm, sid, package, context, "safe-walk", "0,3"), package, context=context
        )
        assert walked.status == "committed", walked
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state.actor_zone[pc] == "0,3"
        assert next(a for a in after.character_states if a.entity_id == NPC).reaction_available


@pytest.mark.parametrize("kind", ["dash", "dodge", "disengage"])
def test_reaction_decision_does_not_block_npc_action_then_walk(
    tmp_path, package_data, context, kind
):
    package, assets = withdrawal_package(package_data, npc_at_reach=True, reviewed_reactions=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / f"pre-walk-{kind}.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context, npc_first=True, distance=20)
        decision = command(
            sm,
            sid,
            package,
            context,
            "pc-react",
            "combat.opportunity",
            CombatOpportunity(NPC, True),
        )
        assert sm.submit_command(decision, package, context=context).status == "committed"
        action = command(
            sm,
            sid,
            package,
            context,
            f"npc-{kind}",
            "combat.intent",
            CombatIntentPayload(intent_type=kind),
            actor=NPC,
        )
        taken = sm.submit_command(action, package, context=context)
        assert taken.status == "committed", taken
        after_action = sm.read_combat_snapshot(sid, package, context=context)
        assert after_action.combat_state.pending_reactions
        assert next(
            a for a in after_action.character_states if a.entity_id == pc
        ).reaction_available
        move = walk(sm, sid, package, context, f"npc-after-{kind}", "3,0", actor=NPC)
        moved = sm.submit_command(move, package, context=context)
        assert moved.status == "committed", moved
        after = sm.read_combat_snapshot(sid, package, context=context)
        reactor = next(a for a in after.character_states if a.entity_id == pc)
        assert reactor.reaction_available is (kind == "disengage")


def test_reaction_decision_does_not_block_npc_attack_before_pass(tmp_path, package_data, context):
    package, assets = withdrawal_package(package_data, npc_at_reach=True, reviewed_reactions=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "attack-before-oa.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(
            sm,
            sid,
            package,
            context,
            npc_first=True,
            distance=20,
            profiles=(CombatOpponent(NPC, "bandit"),),
        )
        decision = command(
            sm,
            sid,
            package,
            context,
            "pc-react",
            "combat.opportunity",
            CombatOpportunity(NPC, True),
        )
        assert sm.submit_command(decision, package, context=context).status == "committed"
        attack = command(
            sm,
            sid,
            package,
            context,
            "npc-first-attack",
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack", target_id=pc, stat_block_action_id="scimitar"
            ),
            actor=NPC,
        )
        attack_receipt = sm.submit_command(attack, package, context=context)
        assert attack_receipt.status == "committed", attack_receipt
        middle = sm.read_combat_snapshot(sid, package, context=context)
        assert middle.combat_state.pending_reactions
        assert next(a for a in middle.character_states if a.entity_id == pc).reaction_available
        passed = command(
            sm,
            sid,
            package,
            context,
            "npc-after-attack",
            "combat.intent",
            CombatIntentPayload(intent_type="pass"),
            actor=NPC,
        )
        receipt = sm.submit_command(passed, package, context=context)
        assert receipt.status == "committed", receipt
        assert not sm.read_combat_snapshot(
            sid, package, context=context
        ).combat_state.pending_reactions


@pytest.mark.parametrize("npc_hp,arrival", [(13, "0,3"), (1, "0,2")])
def test_opportunity_preserves_prefix_and_suffix_or_lethal_stop(
    tmp_path, package_data, context, npc_hp, arrival
):
    package, assets = withdrawal_package(
        package_data, reviewed_reactions=True, reaction_split=True, npc_hp=npc_hp
    )
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / f"segmented-{npc_hp}.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        start_encounter(sm, sid, package, context, npc_first=True, distance=20)
        decision = command(
            sm,
            sid,
            package,
            context,
            "react-to-route",
            "combat.opportunity",
            CombatOpportunity(NPC, True),
        )
        assert sm.submit_command(decision, package, context=context).status == "committed"
        route = walk(sm, sid, package, context, "segmented-route", "0,3", actor=NPC)
        result = sm.submit_command(route, package, context=context)
        assert result.status == "committed", result
        events = [
            event.payload["engine_event"]
            for event in command_events(sm, sid, package, context, result)
            if event.event_type == "combat.engine_event"
        ]
        assert sum(event["type"] == "attack_rolled" for event in events) == 1
        assert sum(event["type"] == "actor_moved" for event in events) == (2 if npc_hp == 13 else 1)
        assert (
            sm.read_combat_snapshot(sid, package, context=context).combat_state.actor_zone[NPC]
            == arrival
        )


def test_spent_opportunity_cannot_fire_on_second_exit(tmp_path, package_data, context):
    package, assets = withdrawal_package(package_data, npc_at_reach=True, reviewed_reactions=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "spent-opportunity.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context, npc_first=True, distance=20)
        decision = command(
            sm,
            sid,
            package,
            context,
            "react-once",
            "combat.opportunity",
            CombatOpportunity(NPC, True),
        )
        assert sm.submit_command(decision, package, context=context).status == "committed"
        first = sm.submit_command(
            walk(sm, sid, package, context, "first-exit", "2,0", actor=NPC),
            package,
            context=context,
        )
        assert first.status == "committed", first
        assert not next(
            a
            for a in sm.read_combat_snapshot(sid, package, context=context).character_states
            if a.entity_id == pc
        ).reaction_available
        assert (
            sm.submit_command(
                walk(sm, sid, package, context, "reenter", "1,0", actor=NPC),
                package,
                context=context,
            ).status
            == "committed"
        )
        rng = sm.read_rng_context(sid, package, context=context)
        second = sm.submit_command(
            walk(sm, sid, package, context, "second-exit", "2,0", actor=NPC),
            package,
            context=context,
        )
        assert second.status == "committed", second
        assert not any(
            event.payload.get("engine_event", {}).get("type") == "attack_rolled"
            for event in command_events(sm, sid, package, context, second)
        )
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_reactive_walk_receipt_failure_rolls_back_damage_and_retries(
    tmp_path, package_data, context
):
    package, assets = withdrawal_package(
        package_data, reviewed_reactions=True, reaction_split=True, npc_hp=1
    )
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "opportunity-rollback.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        start_encounter(sm, sid, package, context, npc_first=True, distance=20)
        decision = command(
            sm,
            sid,
            package,
            context,
            "react-rollback",
            "combat.opportunity",
            CombatOpportunity(NPC, True),
        )
        assert sm.submit_command(decision, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        position = sm.read_actor_position(sid, NPC, package, context=context)
        move = walk(sm, sid, package, context, "reactive-rollback", "0,3", actor=NPC)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_opportunity_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='reactive-rollback' "
            "BEGIN SELECT RAISE(ABORT,'injected opportunity receipt failure'); END"
        )
        try:
            with pytest.raises(StateMachineError):
                sm.submit_command(move, package, context=context)
            assert sm.read_combat_snapshot(sid, package, context=context) == before
            assert sm.read_rng_context(sid, package, context=context) == rng
            assert sm.read_actor_position(sid, NPC, package, context=context) == position
        finally:
            sm._conn.execute("DROP TRIGGER fail_opportunity_receipt")
        receipt = sm.submit_command(move, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert next(a for a in after.character_states if a.entity_id == NPC).hp_current == 0
        assert after.combat_state.actor_zone[NPC] == "0,2"
        assert not after.combat_state.pending_reactions
        assert sm.submit_command(move, package, context=context) == receipt
