"""Exercise paid exit movement through the public encounter lifecycle."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_haste_delta import application_clock
from dnd5e_engine.evaluation_speed_effects import _bound_effect, _spell_speed_definition
from dnd_support import NPC, create, pass_turn
from lock_support import lock_data, lock_loader
from modern_support import modern_data, modern_loader
from test_lockable_passages import lock_state
from test_scoped_runtime_validation import grow_scenes, measure
from test_session_time_coordination import Clock, Scheduler, start, sync
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    CombatOpportunity,
    CombatWithdraw,
    StartCombat,
    StateMachine,
    StateMachineError,
    StepActor,
    TimeBinding,
    TypedCommand,
)
from trpg_state_machine.package import _validate

A, B = "scene.platform", "scene.workshop"
PASSAGE = "passage.mechanical"
OTHER = "npc.attendant"


def withdrawal_package(
    base,
    *,
    closed=False,
    budget=None,
    bystander=False,
    third=False,
    npc_at_passage=False,
    opportunity_reach=False,
    npc_at_reach=False,
    reviewed_reactions=False,
    reaction_split=False,
    npc_hp=13,
    obstacle=False,
):
    if closed:
        data, assets = lock_data(), lock_loader()
        for actor in data["actors"]:
            actor.pop("npc_state", None)
    else:
        assets = modern_loader()
        data = modern_data(base, assets, npc_hp=npc_hp, spatial=True)
    data["time_binding"] = asdict(TimeBinding())
    data["actors"][0]["initial_cell"] = "3,0"
    for scene in data["scenes"]:
        scene["grid"].update(width=6, height=5)
    noncombat = data["rule_bindings"]["noncombat_foundation"]
    noncombat["snapshot"]["scene_state"]["grid"].update(width=6, height=5)
    for scene in noncombat["additional_scenes"]:
        scene["grid"].update(width=6, height=5)
    del data["rule_bindings"]["combat_foundation"]
    if budget is not None:
        noncombat["snapshot"]["character_states"][0].update(
            base_speed=budget, movement_remaining=budget
        )
        data["passages"][0]["endpoints"][0].update(standing_cell="0,1", threshold_cell="0,2")
    if bystander:
        actor = data["actors"][1]
        actor.update(initial_scene_id=A, initial_cell="0,1")
        data["scenes"][0]["actor_ids"].append(actor["actor_id"])
    if third:
        actor = data["actors"][1]
        actor.update(initial_scene_id=A, initial_cell="5,3")
        data["scenes"][0]["actor_ids"].append(actor["actor_id"])
        snapshot = noncombat["snapshot"]
        snapshot["character_states"].append(
            {**deepcopy(snapshot["character_states"][1]), "entity_id": OTHER}
        )
        noncombat["xp"][OTHER] = 0
    if npc_at_passage:
        data["actors"][0]["initial_cell"] = "0,0"
        data["pc_templates"][0]["initial_cell"] = "3,0"
    if opportunity_reach:
        data["actors"][0]["initial_cell"] = "0,0"
        data["pc_templates"][0]["initial_cell"] = "2,0"
        noncombat["snapshot"]["character_states"][1]["melee_reach_ft"] = 10
    if npc_at_reach:
        data["actors"][0]["initial_cell"] = "1,0"
    if reaction_split:
        data["actors"][0]["initial_cell"] = "0,0"
        data["pc_templates"][0]["initial_cell"] = "1,1"
    if reviewed_reactions:
        for actor in noncombat["snapshot"]["character_states"]:
            actor["granted_features"] = []
    if obstacle:
        data["scenes"][0]["grid"]["blocked_cells"] = ["1,0"]
        noncombat["snapshot"]["scene_state"]["grid"]["blocked_cells"] = ["1,0"]
    return _validate(data), assets


def command(sm, sid, package, context, key, kind, payload, *, actor=None):
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(sid, key, actor or state.pc_actor_id, kind, payload, state.world_version)


def start_encounter(
    sm,
    sid,
    package,
    context,
    *,
    area=None,
    distance=None,
    key="open",
    npc_first=False,
    enemies=(NPC,),
    profiles=(),
):
    pc = sm.read_session(sid, package, context=context).pc_actor_id
    roster = (pc, *enemies)
    payload = StartCombat(
        roster,
        roster[::-1] if npc_first else roster,
        opening_cell="0,0" if area is None and distance is None else None,
        area_cells=area,
        opponent_profiles=profiles,
        withdrawal_distance_ft=distance,
    )
    result = sm.submit_command(
        command(sm, sid, package, context, key, "combat.start", payload), package, context=context
    )
    assert result.status == "committed", result
    return pc


def walk(sm, sid, package, context, key, destination, *, actor=None):
    return command(
        sm,
        sid,
        package,
        context,
        key,
        "combat.intent",
        CombatIntentPayload(intent_type="move", target_zone_id=destination),
        actor=actor,
    )


def leave(sm, sid, package, context, key, source, *, target=None, passage=None, actor=None):
    return command(
        sm,
        sid,
        package,
        context,
        key,
        "combat.withdraw",
        CombatWithdraw(A, source, target_cell=target, passage_id=passage),
        actor=actor,
    )


def command_events(sm, sid, package, context, receipt):
    return [
        event
        for event in sm.read_events(sid, package, context=context)
        if event.event_seq in receipt.event_seqs
    ]


def test_explicit_local_exit_charges_one_step_and_allows_next_encounter(
    tmp_path, package_data, context
):
    package, assets = withdrawal_package(package_data)
    context = replace(context, can_start_combat=True, can_advance_time=True)
    path = tmp_path / "local-withdraw.db"
    with StateMachine(path, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        clock = Clock()
        timeline = sm, sid, package, context, clock, Scheduler(clock)
        start(timeline)
        clock.advance(4)
        pc = start_encounter(sm, sid, package, context, area=("0,0", "0,1", "3,0"))
        initial_id = sm.read_combat_region(sid, package, context=context).combat_id
        assert sync(timeline).committed_tick == 4
        clock.advance(50)
        boundary = walk(sm, sid, package, context, "inside", "0,1")
        assert sm.submit_command(boundary, package, context=context).status == "committed"
        assert sync(timeline).state == "combat"
        assert sync(timeline).committed_tick == 4
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_hp = before.character_states[0].hp_current
        before_rng = sm.read_rng_context(sid, package, context=context)

        ordinary_outside = walk(sm, sid, package, context, "ordinary-outside", "0,2")
        assert sm.submit_command(ordinary_outside, package, context=context).status == "rejected"
        distant_exit = leave(sm, sid, package, context, "too-far-outside", "0,1", target="0,3")
        assert sm.submit_command(distant_exit, package, context=context).status == "rejected"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        exit_attempt = leave(sm, sid, package, context, "outside", "0,1", target="0,2")
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_withdrawal BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='outside' "
            "BEGIN SELECT RAISE(ABORT,'injected withdrawal write failure'); END"
        )
        try:
            with pytest.raises(StateMachineError):
                sm.submit_command(exit_attempt, package, context=context)
            assert sm.read_combat_snapshot(sid, package, context=context) == before
            assert sm.read_actor_position(sid, pc, package, context=context).cell == "0,1"
            assert sync(timeline).state == "combat"
        finally:
            sm._conn.execute("DROP TRIGGER fail_withdrawal")

        receipt = sm.submit_command(exit_attempt, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.read_actor_position(sid, pc, package, context=context).cell == "0,2"
        assert sm.read_combat_region(sid, package, context=context) is None
        ended = sm.read_combat_snapshot(sid, package, context=context)
        assert ended.combat_state.ended
        assert ended.combat_state.departed_actor_id == pc
        assert ended.combat_state.movement_ledgers[pc].distance_ft == 10
        assert ended.character_states[0].hp_current == before_hp
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        events = command_events(sm, sid, package, context, receipt)
        assert [event.event_type for event in events] == ["combat.engine_event", "combat.withdrawn"]
        assert events[0].payload["engine_event"]["path"] == ["0,1", "0,2"]
        assert events[1].payload["reason"] == "withdrawal"
        assert sync(timeline).state == "running"
        assert sync(timeline).committed_tick == sync(timeline).estimated_target_tick == 4
        clock.advance(2)
        assert sync(timeline).estimated_target_tick == 6
        assert sm.submit_command(exit_attempt, package, context=context) == receipt

    with StateMachine(path, dnd_loader=assets) as sm:
        assert sm.submit_command(exit_attempt, package, context=context) == receipt
        step = command(
            sm,
            sid,
            package,
            context,
            "explore-after-exit",
            "world.step_actor",
            StepActor(A, "0,2", "0,3"),
        )
        assert sm.submit_command(step, package, context=context).status == "committed"
        start_encounter(sm, sid, package, context, area=("0,3", "3,0"), key="open-again")
        next_region = sm.read_combat_region(sid, package, context=context)
        assert next_region.combat_id != initial_id
        current = sm.read_combat_snapshot(sid, package, context=context)
        assert current.combat_state.actor_zone[pc] == "0,3"
        assert current.character_states[0].hp_current == before_hp
        assert sm.submit_command(exit_attempt, package, context=context) == receipt


@pytest.mark.parametrize("closed", [False, True])
def test_combat_passage_respects_world_door_and_commits_actual_landing(
    tmp_path, package_data, context, closed
):
    package, assets = withdrawal_package(package_data, closed=closed)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "passage-withdraw.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context)
        attempt = leave(sm, sid, package, context, "passage-exit", "0,0", passage=PASSAGE)
        baseline = sm.read_combat_snapshot(sid, package, context=context)
        if not closed:
            world_before = sm.read_session(sid, package, context=context)
            events_before = sm.read_events(sid, package, context=context)
            sm._conn.execute(
                "CREATE TEMP TRIGGER fail_passage_exit BEFORE INSERT ON command_receipts "
                "WHEN NEW.command_id='passage-exit' "
                "BEGIN SELECT RAISE(ABORT,'injected passage withdrawal failure'); END"
            )
            try:
                with pytest.raises(StateMachineError):
                    sm.submit_command(attempt, package, context=context)
                assert sm.read_combat_snapshot(sid, package, context=context) == baseline
                assert sm.read_session(sid, package, context=context) == world_before
                assert sm.read_events(sid, package, context=context) == events_before
            finally:
                sm._conn.execute("DROP TRIGGER fail_passage_exit")
        receipt = sm.submit_command(attempt, package, context=context)
        assert sm.submit_command(attempt, package, context=context) == receipt
        if closed:
            assert (receipt.status, receipt.error_code) == ("rejected", "passage_unavailable")
            assert lock_state(sm, sid) == ("closed", 1)
            assert sm.read_combat_snapshot(sid, package, context=context) == baseline
            assert sm.read_actor_position(sid, pc, package, context=context).scene_id == A
            return
        assert receipt.status == "committed", receipt
        landing = sm.read_actor_position(sid, pc, package, context=context)
        assert (landing.scene_id, landing.cell) == (B, "0,0")
        assert sm.read_session(sid, package, context=context).current_scene_id == B
        ended = sm.read_combat_snapshot(sid, package, context=context).combat_state
        assert ended.ended and ended.actor_zone[pc] == "0,1"
        assert ended.movement_ledgers[pc].distance_ft == 5
        events = command_events(sm, sid, package, context, receipt)
        assert [event.event_type for event in events] == [
            "combat.engine_event",
            "scene.exited",
            "scene.entered",
            "combat.withdrawn",
        ]
        assert events[-1].payload["scene_id"] == B


def test_npc_cannot_enter_outside_cell_as_active_combatant(tmp_path, package_data, context):
    package, assets = withdrawal_package(package_data)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "npc-boundary.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        start_encounter(sm, sid, package, context, area=("0,0", "3,0"), npc_first=True)
        before = sm.read_combat_snapshot(sid, package, context=context)
        attempt = walk(sm, sid, package, context, "npc-outside", "3,1", actor=NPC)
        receipt = sm.submit_command(attempt, package, context=context)
        assert receipt.status == "rejected"
        assert sm.read_combat_snapshot(sid, package, context=context) == before


def test_fixed_area_never_routes_out_and_back(tmp_path, package_data, context):
    package, assets = withdrawal_package(package_data)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "fixed-route.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        start_encounter(
            sm, sid, package, context, area=("0,0", "0,1", "1,1", "2,1", "2,0", "3,0", "0,3")
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        disconnected = walk(sm, sid, package, context, "disconnected", "0,3")
        assert sm.submit_command(disconnected, package, context=context).status == "rejected"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        detour = walk(sm, sid, package, context, "detour", "2,0")
        receipt = sm.submit_command(detour, package, context=context)
        assert receipt.status == "committed", receipt
        (event,) = command_events(sm, sid, package, context, receipt)
        assert event.payload["engine_event"]["path"] == ["0,0", "0,1", "1,1", "2,1", "2,0"]
        assert sm.read_combat_region(sid, package, context=context) is not None


@pytest.mark.parametrize("inside_corner", [False, True])
def test_fixed_area_walk_cannot_cut_an_outside_corner(
    tmp_path, package_data, context, inside_corner
):
    package, assets = withdrawal_package(package_data)
    context = replace(context, can_start_combat=True)
    area = ("0,0", "1,1", "3,0")
    if inside_corner:
        area += ("1,0", "0,1")
    with StateMachine(tmp_path / "fixed-corners.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context, area=area)
        before = sm.read_combat_snapshot(sid, package, context=context)
        move = walk(sm, sid, package, context, "diagonal", "1,1")
        receipt = sm.submit_command(move, package, context=context)
        if inside_corner:
            assert receipt.status == "committed", receipt
            after = sm.read_combat_snapshot(sid, package, context=context)
            assert after.combat_state.movement_ledgers[pc].distance_ft == 5
            assert after.combat_state.actor_zone[pc] == "1,1"
        else:
            assert (receipt.status, receipt.error_code) == ("rejected", "movement.blocked")
            assert sm.read_combat_snapshot(sid, package, context=context) == before


@pytest.mark.parametrize(
    ("fixed", "obstacle"),
    [(False, False), (True, False), (True, True)],
    ids=["unmasked-clear", "fixed-clear", "fixed-real-obstacle"],
)
def test_fixed_path_constraint_does_not_hide_actual_reach(
    tmp_path, package_data, context, fixed, obstacle
):
    package, assets = withdrawal_package(package_data, opportunity_reach=True, obstacle=obstacle)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "fixed-reach.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        start_encounter(
            sm,
            sid,
            package,
            context,
            area=("0,0", "2,0", "3,0") if fixed else None,
            distance=None if fixed else 20,
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        move = walk(sm, sid, package, context, "leave-reach", "3,0")
        receipt = sm.submit_command(move, package, context=context)
        if obstacle:
            assert receipt.status == "committed", receipt
            current = sm.read_combat_snapshot(sid, package, context=context)
            assert current.combat_state.actor_zone[move.actor_id] == "3,0"
        else:
            assert (receipt.status, receipt.error_code) == (
                "rejected",
                "movement.opportunity_attack",
            )
            assert sm.read_combat_snapshot(sid, package, context=context) == before


def test_npc_exit_hands_off_then_last_enemy_closes_without_rewards(tmp_path, package_data, context):
    package, assets = withdrawal_package(package_data, third=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "npc-withdraw.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(
            sm, sid, package, context, area=("0,0", "3,0", "5,3"), enemies=(OTHER, NPC)
        )
        wrong_turn = leave(sm, sid, package, context, "wrong-turn", "3,0", target="3,1", actor=NPC)
        assert (
            sm.submit_command(wrong_turn, package, context=context).error_code == "not_actor_turn"
        )
        pass_turn(sm, sid, package, context, "pc-ends")
        pass_turn(sm, sid, package, context, "other-ends")
        before = sm.read_combat_snapshot(sid, package, context=context)
        departure = leave(sm, sid, package, context, "npc-exits", "3,0", target="3,1", actor=NPC)
        with pytest.raises(StateMachineError, match="Trusted NPC control"):
            sm.submit_command(departure, package, context=replace(context, controlled_npc_ids=()))
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_npc_exit BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='npc-exits' "
            "BEGIN SELECT RAISE(ABORT,'injected NPC exit failure'); END"
        )
        try:
            with pytest.raises(StateMachineError):
                sm.submit_command(departure, package, context=context)
            assert sm.read_combat_snapshot(sid, package, context=context) == before
        finally:
            sm._conn.execute("DROP TRIGGER fail_npc_exit")
        receipt = sm.submit_command(departure, package, context=context)
        assert receipt.status == "committed", receipt
        current = sm.read_combat_snapshot(sid, package, context=context).combat_state
        assert current.initiative_ids == (pc, OTHER)
        assert (
            current.initiative_ids[current.current_turn_index] == pc and current.round_number == 2
        )
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 6
        assert sm.read_actor_position(sid, NPC, package, context=context).cell == "3,1"
        assert NPC not in current.movement_ledgers and NPC not in current.xp_value_by_entity
        assert sm.submit_command(departure, package, context=context) == receipt
        events = command_events(sm, sid, package, context, receipt)
        assert any(
            e.event_type == "combat.withdrawn" and not e.payload["encounter_ended"] for e in events
        )
        assert any(
            e.payload.get("engine_event", {}).get("type") == "turn_started"
            and e.payload["engine_event"]["actor_id"] == pc
            for e in events
        )
        pass_turn(sm, sid, package, context, "next-actor")
        last = leave(sm, sid, package, context, "last-exits", "5,3", target="4,3", actor=OTHER)
        last_receipt = sm.submit_command(last, package, context=context)
        assert last_receipt.status == "committed", last_receipt
        ended = sm.read_combat_snapshot(sid, package, context=context).combat_state
        assert ended.ended and ended.departed_actor_id == OTHER and not ended.dead_ids
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 6
        assert sm.read_actor_position(sid, OTHER, package, context=context).cell == "4,3"
        assert not any(
            e.payload.get("engine_event", {}).get("type") == "combat_ended"
            for e in command_events(sm, sid, package, context, last_receipt)
        )


@pytest.mark.parametrize(
    ("third", "accept"),
    [(False, False), (True, False), (True, True)],
    ids=["declined-ends", "declined-handoff", "accepted-blocks"],
)
def test_fixed_withdrawal_respects_pc_opportunity_decision(
    tmp_path, package_data, context, third, accept
):
    package, assets = withdrawal_package(
        package_data, npc_at_reach=True, reviewed_reactions=True, third=third
    )
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "withdrawal-reaction.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(
            sm,
            sid,
            package,
            context,
            area=("0,0", "1,0", "5,3") if third else ("0,0", "1,0"),
            enemies=(NPC, OTHER) if third else (NPC,),
            npc_first=True,
        )
        if third:
            pass_turn(sm, sid, package, context, "other-ends")
        decision = command(
            sm,
            sid,
            package,
            context,
            "reaction-decision",
            "combat.opportunity",
            CombatOpportunity(NPC, accept),
        )
        assert sm.submit_command(decision, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        position = sm.read_actor_position(sid, NPC, package, context=context)
        attempt = leave(sm, sid, package, context, "npc-exits", "1,0", target="2,0", actor=NPC)
        assert len(before.combat_state.pending_reactions) == 1

        if third and not accept:
            combat_query = "SELECT state_json FROM combats WHERE session_id=?"
            persisted = sm._conn.execute(combat_query, (sid,)).fetchone()[0]
            sm._conn.execute(
                "CREATE TEMP TRIGGER fail_withdrawal_receipt BEFORE INSERT ON command_receipts "
                "WHEN NEW.command_id='npc-exits' "
                "AND json_extract(NEW.receipt_json, '$.status') = 'committed' "
                "BEGIN SELECT RAISE(ABORT,'injected withdrawal receipt failure'); END"
            )
            try:
                with pytest.raises(StateMachineError, match="SQLite transaction failed") as exc:
                    sm.submit_command(attempt, package, context=context)
                assert "injected withdrawal receipt failure" in str(exc.value.__cause__)
                assert sm.read_combat_snapshot(sid, package, context=context) == before
                assert sm.read_rng_context(sid, package, context=context) == rng
                assert sm.read_actor_position(sid, NPC, package, context=context) == position
                assert sm._conn.execute(combat_query, (sid,)).fetchone()[0] == persisted
            finally:
                sm._conn.execute("DROP TRIGGER fail_withdrawal_receipt")

        receipt = sm.submit_command(attempt, package, context=context)
        if accept:
            assert (receipt.status, receipt.error_code) == (
                "rejected",
                "combat.withdrawal_effects_unsupported",
            )
            assert not receipt.event_seqs
            assert sm.read_combat_snapshot(sid, package, context=context) == before
            assert sm.read_rng_context(sid, package, context=context) == rng
            assert sm.read_actor_position(sid, NPC, package, context=context) == position
            return

        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        combat = after.combat_state
        assert not combat.pending_reactions
        assert next(
            actor for actor in after.character_states if actor.entity_id == pc
        ).reaction_available
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_actor_position(sid, NPC, package, context=context).cell == "2,0"
        events = command_events(sm, sid, package, context, receipt)
        engine_events = [
            event.payload["engine_event"]
            for event in events
            if event.event_type == "combat.engine_event"
        ]
        assert [
            event["movement_cost_ft"] for event in engine_events if event["type"] == "actor_moved"
        ] == [5]
        assert not any(event["type"] == "attack_rolled" for event in engine_events)
        assert [
            event["actor_id"] for event in engine_events if event["type"] == "turn_started"
        ] == ([pc] if third else [])
        assert any(
            event.event_type == "combat.withdrawn" and event.payload["encounter_ended"] is not third
            for event in events
        )
        if third:
            assert not combat.ended and combat.initiative_ids == (OTHER, pc)
            assert combat.initiative_ids[combat.current_turn_index] == pc
            assert NPC not in combat.movement_ledgers
        else:
            assert combat.ended and combat.departed_actor_id == NPC
        assert sm.submit_command(attempt, package, context=context) == receipt
        assert sm.read_combat_snapshot(sid, package, context=context) == after


def test_distance_withdrawal_requires_nearest_hostile_distance_and_spends_no_move(
    tmp_path, package_data, context
):
    package, assets = withdrawal_package(package_data, third=True)
    context = replace(context, can_start_combat=True)
    db = tmp_path / "distance-withdraw.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context, distance=20, enemies=(NPC, OTHER))
        before = sm.read_combat_snapshot(sid, package, context=context)
        close = leave(sm, sid, package, context, "too-close", "0,0")
        assert (
            sm.submit_command(close, package, context=context).error_code
            == "combat.withdrawal_distance"
        )
        fixed_kind = leave(sm, sid, package, context, "wrong-kind", "0,0", target="0,1")
        assert sm.submit_command(fixed_kind, package, context=context).status == "rejected"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        march = walk(sm, sid, package, context, "separate", "0,4")
        assert sm.submit_command(march, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        attempt = leave(sm, sid, package, context, "distance", "0,4")
        receipt = sm.submit_command(attempt, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state.movement_ledgers == before.combat_state.movement_ledgers
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_actor_position(sid, pc, package, context=context).cell == "0,4"
        assert [e.event_type for e in command_events(sm, sid, package, context, receipt)] == [
            "combat.withdrawn"
        ]
    with StateMachine(db, dnd_loader=assets) as sm:
        assert sm.submit_command(attempt, package, context=context) == receipt
        start_encounter(sm, sid, package, context, area=("0,4", "3,0"), key="fixed-now")
        wrong_policy = leave(sm, sid, package, context, "distance-from-fixed", "0,4")
        assert (
            sm.submit_command(wrong_policy, package, context=context).error_code
            == "combat.withdrawal_unavailable"
        )
        assert sm.submit_command(attempt, package, context=context) == receipt


@pytest.mark.parametrize("distance", [False, True], ids=["scene-passage", "distance"])
def test_npc_can_explicitly_withdraw_without_ending_other_opponents(
    tmp_path, package_data, context, distance
):
    package, assets = withdrawal_package(package_data, third=True, npc_at_passage=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "npc-other-policy.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(
            sm,
            sid,
            package,
            context,
            distance=15 if distance else None,
            npc_first=True,
            enemies=(OTHER, NPC),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        departure = leave(
            sm,
            sid,
            package,
            context,
            "npc-other-mode",
            "0,0",
            passage=None if distance else PASSAGE,
            actor=NPC,
        )
        receipt = sm.submit_command(departure, package, context=context)
        assert receipt.status == "committed", receipt
        current = sm.read_combat_snapshot(sid, package, context=context).combat_state
        assert current.initiative_ids == (OTHER, pc) and current.current_turn_index == 0
        assert current.round_number == before.combat_state.round_number
        landed = sm.read_actor_position(sid, NPC, package, context=context)
        assert (landed.scene_id, landed.cell) == (A if distance else B, "0,0")
        assert sm.read_session(sid, package, context=context).current_scene_id == A
        assert sm.read_rng_context(sid, package, context=context) == rng
        events = command_events(sm, sid, package, context, receipt)
        actor_moved = any(
            e.payload.get("engine_event", {}).get("type") == "actor_moved" for e in events
        )
        assert actor_moved == (not distance)
        assert sm.submit_command(departure, package, context=context) == receipt


@pytest.mark.parametrize("npc_first", [False, True])
def test_passage_requires_unspent_walk_and_the_current_turn(
    tmp_path, package_data, context, npc_first
):
    package, assets = withdrawal_package(package_data, budget=None if npc_first else 5)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "unpaid-withdrawal.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = start_encounter(sm, sid, package, context, npc_first=npc_first)
        expected_cell = "0,0" if npc_first else "0,1"
        if not npc_first:
            move = walk(sm, sid, package, context, "last-available-step", expected_cell)
            assert sm.submit_command(move, package, context=context).status == "committed"
        attempt = leave(sm, sid, package, context, "unpaid", expected_cell, passage=PASSAGE)
        before = sm.read_combat_snapshot(sid, package, context=context)
        receipt = sm.submit_command(attempt, package, context=context)
        assert receipt.status == "rejected", receipt
        assert receipt.error_code == ("not_actor_turn" if npc_first else "movement.insufficient")
        assert not receipt.event_seqs
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_actor_position(sid, pc, package, context=context).cell == expected_cell


def test_bystander_occupied_exit_is_not_a_combatant_or_a_legal_landing(
    tmp_path, package_data, context
):
    package, assets = withdrawal_package(package_data, bystander=True)
    context = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "occupied-withdrawal.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        start_encounter(sm, sid, package, context, area=("0,0", "3,0"))
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert "npc.attendant" not in before.combat_state.initiative_ids
        attempt = leave(sm, sid, package, context, "occupied", "0,0", target="0,1")
        receipt = sm.submit_command(attempt, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "movement.capability")
        assert sm.read_combat_snapshot(sid, package, context=context) == before


def test_boundary_adoption_scope_does_not_grow_with_unrelated_scenes(
    tmp_path, package_data, context, record_property
):
    package, assets = withdrawal_package(package_data)
    context = replace(context, can_start_combat=True)
    samples = []
    for extra_scenes in (0, 32):
        with StateMachine(
            tmp_path / f"withdraw-locality-{extra_scenes}.db", dnd_loader=assets
        ) as sm:
            sid = create(sm, package, context)
            with sm._transaction(write=True):
                sm._world.scenes.ensure_scene(sid, B, package)
            grow_scenes(sm, sid, extra_scenes)
            start_encounter(sm, sid, package, context, area=("0,0", "3,0"))
            attempt = leave(sm, sid, package, context, "scoped-exit", "0,0", target="0,1")
            with measure(sm) as (counts, statements):
                receipt = sm.submit_command(attempt, package, context=context)
            assert receipt.status == "committed", receipt
            actor_route_query = sum(
                "SELECT actor_id,cell FROM actors" in sql and "json_each" in sql
                for sql in statements
            )
            assert actor_route_query == 1
            samples.append((counts["sql_selects"], counts["sql_rows"]))
    assert samples[0] == samples[1], samples
    record_property("withdrawal_sql_selects_and_rows", samples[0])


def test_withdrawal_preserves_unmapped_combat_effect_clock(timed_spell_world):
    def configure(data):
        del data["rule_bindings"]["combat_foundation"]
        data["actors"][0]["initial_cell"] = "7,0"

    sm, sid, package, context = timed_spell_world(
        walking=True, hit_rider="ray-of-frost", configure=configure
    )
    context = replace(context, can_start_combat=True)
    with sm:
        pc = start_encounter(sm, sid, package, context, area=("0,0", "0,1", "7,0"), npc_first=True)
        after_start = sm.read_combat_snapshot(sid, package, context=context).combat_state
        # Seed the exact bound opposing rider at its source turn, then use public Pass.
        effects = sm._characters.effects
        definition = _spell_speed_definition(sm._world.loader, "ray-of-frost")
        rider = _bound_effect(definition, NPC, pc)
        clock = application_clock(rider, after_start.turn_serial, after_start.round_number)
        with sm._transaction(write=True):
            effects.add_effect(
                sid,
                rider.model_dump(mode="json"),
                conditions=(),
                lifecycle=clock.model_dump(mode="json")["state"],
            )
            sm._characters.update_rule_metadata(sid, pc, "movement_remaining", 20)
        pass_turn(sm, sid, package, context, "rider-source-passes")
        inside = walk(sm, sid, package, context, "rider-inside", "0,1")
        assert sm.submit_command(inside, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert before.combat_state.effect_lifecycles == (clock,)
        attempt = leave(sm, sid, package, context, "rider-withdrawal", "0,1", target="0,2")
        receipt = sm.submit_command(attempt, package, context=context)
        assert (receipt.status, receipt.error_code) == (
            "rejected",
            "combat.withdrawal_effects_unsupported",
        )
        assert not receipt.event_seqs
        assert sm.read_combat_snapshot(sid, package, context=context) == before
