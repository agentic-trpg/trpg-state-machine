"""Exercise start damage, automatic handoff and receipts through public combat commands."""

from dataclasses import replace

import pytest
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_haste_delta import application_clock, canonical_haste_effect
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import NPC, create
from test_combat_start import opening_data, start_command
from test_dnd_ledger_scope import stored_state
from test_dnd_npc_availability import npc_command
from test_hold_effect_candidate import pass_command

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.package import _validate

ALCHEMISTS_FIRE = "alchemists-fire"


def turn_assets():
    """Load canonical locked assets used by the encounter and its two effects."""
    bundled = BundledAssetLoader()
    return MemoryAssetLoader(
        items=[
            *(bundled.get_weapon(slug) for slug in ("rapier", "longsword", "dagger", "greatsword")),
            bundled.get_item(ALCHEMISTS_FIRE),
        ],
        spells=[bundled.get_spell("haste")],
        conditions=[bundled.get_condition("incapacitated")],
        monsters=[bundled.get_monster("bandit")],
    )


def turn_package(base, assets):
    """Create a live opening roster with positive HP and a fixed repeatable RNG."""
    data = opening_data(base, assets)
    data["pc_templates"][0]["character"]["hp"]["current"] = 1
    snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
    snapshot["snapshot_schema_version"] = "engine-snapshot/20"
    for participant in snapshot["character_states"]:
        assert not participant.pop("spells_known")
    snapshot["character_states"][0].update(
        hp_current=1, ac=1, weapon_in_hands=None, weapon_grip="none"
    )
    return _validate(data)


def seed_burning(sm, sid, pc, assets):
    """Load an existing Alchemist's Fire application through the Effects owner."""
    definition = assets.get_item(ALCHEMISTS_FIRE).passive_effects[0]
    active = passive_effect_to_active_effect(definition, target_id=pc, caster_id=NPC)
    effect = effect_state(active.model_copy(update={"origin": f"item:{ALCHEMISTS_FIRE}:{NPC}"}))
    with sm._transaction(write=True):
        sm._characters.effects.add_effect(sid, effect.model_dump(mode="json"))
    return effect


def seed_lethargy(sm, sid, pc, combat):
    """Load the canonical Haste follow-up and its previous-turn application clock."""
    (effect,) = canonical_haste_effect(NPC, pc).end_effects
    record = application_clock(effect, combat.turn_serial, combat.round_number)
    with sm._transaction(write=True):
        sm._characters.effects.add_effect(
            sid,
            effect.model_dump(mode="json"),
            conditions=sorted(effect.statuses),
            lifecycle=record.model_dump(mode="json")["state"],
        )
    return effect


def actor(snapshot, actor_id):
    """Read one actor from the public mechanical snapshot."""
    return next(entry for entry in snapshot.character_states if entry.entity_id == actor_id)


def receipt_events(sm, sid, package, host, receipt):
    """Read ordered rules evidence and bind it to the original command receipt."""
    assert receipt.status == "committed", receipt
    events = [
        event
        for event in sm.read_events(sid, package, context=host)
        if event.source_command_id == receipt.command_id
    ]
    assert tuple(event.event_seq for event in events) == receipt.event_seqs
    assert sm.get_command_receipt(sid, receipt.command_id, package, context=host) == receipt
    return [event.payload["engine_event"] for event in events if "engine_event" in event.payload]


def _active_turn(snapshot):
    """Read the actual current initiative actor and its serial and round."""
    turn = snapshot.combat_state
    return (turn.initiative_ids[turn.current_turn_index], turn.turn_serial, turn.round_number)


def _phase(events, phase, actor_id):
    """Locate the sole phase marker for that actor in this receipt."""
    (index,) = tuple(
        i
        for i, event in enumerate(events)
        if event["type"] == "turn_phase"
        and event["phase"] == phase
        and event["actor_id"] == actor_id
    )
    return index


def _assert_start_damage(events, pc, rng_before, rng_after):
    """Observe actual TurnStart damage and knockout before that actor's actual TurnEnd."""
    ((damage_at, damage),) = (
        (i, event)
        for i, event in enumerate(events)
        if event["type"] == "damage_applied" and event["target_id"] == pc
    )
    (unconscious_at,) = tuple(
        i
        for i, event in enumerate(events)
        if event["type"] == "condition_applied"
        and event["target_id"] == pc
        and event["condition"] == "unconscious"
    )
    assert damage["damage_type"] == "fire"
    assert damage["source_actor_id"] == NPC
    assert damage["source_id"] == f"item:{ALCHEMISTS_FIRE}"
    expected_rng = rng_before.state.restore()
    assert damage["amount"] == expected_rng.randint(1, 4)
    assert rng_after.version == rng_before.version + 1
    assert rng_after.state == RNGState.capture(expected_rng)
    end = _phase(events, "turn_end", pc)
    assert _phase(events, "turn_start", pc) < damage_at < unconscious_at < end
    assert not any(event["type"] == "death_save_rolled" for event in events)
    return end


def test_pass_start_damage_runs_turn_end_before_round_handoff_atomically(
    tmp_path, package_data, context, monkeypatch
):
    """A skipped PC Turn still expires Lethargy before handing off at the wrap."""
    assets = turn_assets()
    package = turn_package(package_data, assets)
    host = replace(context, can_start_combat=True)
    db = tmp_path / "turn-pass.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        pc = sm.read_session(sid, package, context=host).pc_actor_id
        opening = start_command(sm, sid, package, host)
        assert sm.submit_command(opening, package, context=host).status == "committed"
        initial = sm.read_combat_snapshot(sid, package, context=host)
        assert _active_turn(initial) == (NPC, 1, 1)
        burning = seed_burning(sm, sid, pc, assets)
        lethargy = seed_lethargy(sm, sid, pc, initial.combat_state)
        before = sm.read_combat_snapshot(sid, package, context=host)
        assert before.effect_states == (burning, lethargy)
        assert actor(before, pc).hp_current == 1
        assert [c.condition for c in actor(before, pc).conditions] == ["incapacitated"]
        rng_before = sm.read_rng_context(sid, package, context=host)
        tick_before = sm.world_time.read(sid, package, context=host).game_tick
        persisted = stored_state(db)
        command = pass_command(sid, before, "npc.pass.into.burning")
        evaluate = sm._rules._evaluate_dnd
        proposals = []

        async def observe(request):
            """Retain the genuine proposed result across the late failure and retry."""
            result = await evaluate(request)
            proposals.append(result)
            return result

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", observe)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_turn_receipt BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT,'turn receipt fault'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=host)
        assert exc.value.code == "storage_error"
        (faulted_proposal,) = proposals
        assert faulted_proposal.status == "accepted", faulted_proposal.error
        assert stored_state(db) == persisted
        assert sm.read_combat_snapshot(sid, package, context=host) == before
        assert sm.read_rng_context(sid, package, context=host) == rng_before
        assert sm.world_time.read(sid, package, context=host).game_tick == tick_before
        assert sm.get_command_receipt(sid, command.command_id, package, context=host) is None
        sm._conn.execute("DROP TRIGGER fail_turn_receipt")

        receipt = sm.submit_command(command, package, context=host)
        events = receipt_events(sm, sid, package, host, receipt)
        assert proposals == [faulted_proposal, faulted_proposal]
        assert events == [
            event.model_dump(mode="json") for event in faulted_proposal.proposed_events
        ]
        rng = sm.read_rng_context(sid, package, context=host)
        end = _assert_start_damage(events, pc, rng_before, rng)
        ((expiry_at, expiry),) = tuple(
            (i, event)
            for i, event in enumerate(events)
            if event["type"] == "effect_expired" and event["effect_id"] == lethargy.id
        )
        assert (expiry["target_id"], expiry["origin"]) == (pc, lethargy.origin)
        assert end < expiry_at < _phase(events, "round_start", None)
        assert _phase(events, "round_start", None) < _phase(events, "turn_start", NPC)
        assert _phase(events, "turn_end", NPC) < _phase(events, "turn_start", pc)
        after = sm.read_combat_snapshot(sid, package, context=host)
        assert _active_turn(after) == (NPC, 3, 2)
        assert after.effect_states == (burning,)
        assert not after.combat_state.ended
        reduced = actor(after, pc)
        assert (reduced.hp_current, reduced.is_alive) == (0, True)
        assert [condition.condition for condition in reduced.conditions] == ["unconscious"]
        assert sm.world_time.read(sid, package, context=host).game_tick == tick_before + 6
        committed = stored_state(db)
        assert sm.submit_command(command, package, context=host) == receipt
        assert stored_state(db) == committed

    with StateMachine(db, dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=host) == after
        assert reopened.read_rng_context(sid, package, context=host) == rng
        assert reopened.submit_command(command, package, context=host) == receipt
        assert receipt_events(reopened, sid, package, host, receipt) == events


def test_zero_hp_at_entry_rolls_death_save_without_competing_start_effect(
    tmp_path, package_data, context
):
    """An actual NPC attack establishes the eligible PC state before Pass."""
    assets = turn_assets()
    package = turn_package(package_data, assets)
    host = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "entry-zero.db", dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        pc = sm.read_session(sid, package, context=host).pc_actor_id
        opening = start_command(sm, sid, package, host)
        assert sm.submit_command(opening, package, context=host).status == "committed"
        attack = npc_command(sm, sid, package, host)
        attack_receipt = sm.submit_command(attack, package, context=host)
        assert attack_receipt.status == "committed", attack_receipt
        before = sm.read_combat_snapshot(sid, package, context=host)
        assert _active_turn(before) == (NPC, 1, 1)
        source = actor(before, pc)
        assert (source.hp_current, source.is_alive) == (0, True)
        assert source.death_saves is None
        assert [condition.condition for condition in source.conditions] == ["unconscious"]
        assert not before.effect_states
        rng_before = sm.read_rng_context(sid, package, context=host)
        command = pass_command(sid, before, "npc.pass.into.death.save")
        receipt = sm.submit_command(command, package, context=host)
        events = receipt_events(sm, sid, package, host, receipt)
        ((roll_at, roll),) = tuple(
            (i, event)
            for i, event in enumerate(events)
            if event["type"] == "death_save_rolled" and event["target_id"] == pc
        )
        assert _phase(events, "turn_start", pc) < roll_at
        expected_rng = rng_before.state.restore()
        assert roll["roll_total"] == expected_rng.randint(1, 20)
        rng = sm.read_rng_context(sid, package, context=host)
        assert rng.version == rng_before.version + 1
        assert rng.state == RNGState.capture(expected_rng)
        assert sm.submit_command(command, package, context=host) == receipt
