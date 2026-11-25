"""Exercise a spell hit rider through public cast, walking and Pass commands."""

from dataclasses import replace
from random import Random

import pytest
from dnd5e_engine import evaluation_delta as delta
from dnd_support import initialize
from item_support import NPC, A
from spatial_support import submit as submit_world
from test_cure_wounds import actor
from test_dnd_runtime import authority
from test_hold_effect_candidate import pass_command
from test_ordinary_spells import assert_committed_events, observe_evaluation
from test_scoped_runtime_validation import grow_scenes, measure
from test_session_time_coordination import submit
from test_spell_attack_cast import OTHER_ENEMY, cast_command
from test_spell_attack_cast import open_spell_combat as open_spell_combat
from test_timed_spell_cast import cast_command as world_cast_command
from test_timed_spell_cast import move_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    AdvanceSessionTime,
    StateMachine,
    StateMachineError,
    StepActor,
    effect_time,
)

RAY = "ray-of-frost"


def effect_rows(sm, sid):
    """Read both effect-owned tables from SQLite after a public operation."""
    return tuple(
        tuple(
            tuple(row)
            for row in sm._conn.execute(f"SELECT * FROM {table} WHERE session_id=?", (sid,))
        )
        for table in ("ongoing_effects", "effect_concentrations")
    )


def test_speed_rider_rejects_elapsed_modifier_on_another_target(timed_spell_world):
    """Refuse an elapsed speed dependency outside the reviewed same-target combination."""
    sm, sid, package, context = timed_spell_world(seed=16, walking=True, hit_rider=RAY)
    with sm:
        cast = world_cast_command((sm, sid, package, context))
        assert (
            receipt := sm.submit_command(cast, package, context=context)
        ).status == "committed", receipt
        assert (receipt := initialize(sm, sid, package, context)).status == "committed", receipt
        command = cast_command(sm, sid, package, context, RAY)
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert len(before.effect_states) == 1 and before.effect_states[0].target_id == cast.actor_id
        stored = authority(sm, sid), effect_rows(sm, sid)
        stored[0].pop("command_receipts")
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "spell.capability"), receipt
        assert receipt.event_seqs == ()
        assert receipt.world_version == before.world_version
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        after = authority(sm, sid)
        after.pop("command_receipts")
        assert (after, effect_rows(sm, sid)) == stored


def test_wizard_five_cannot_supply_wizard_one_attack_proficiency(timed_spell_world):
    """Reject the obsolete +2 override at authored combat dependency admission."""
    with pytest.raises(StateMachineError, match="Wizard proficiency"):
        timed_spell_world(walking=True, hit_rider=RAY, proficiency_override=2)


def retry_clock_expiry_after_growth(sm, command, package, context, monkeypatch):
    """Fail a public Pass after exact expiry, grow remote scenes, and retry on the same facts."""
    sid = command.session_id
    target = NPC
    samples = []
    refresh = effect_time.refresh_world_walk_expiry

    def observed_refresh(world, sid, expired):
        """Count just the new targeted budget maintenance reached from the clock consumer."""
        memory = sm._store.memory
        parsed = memory.stats["parsed_builds"]
        snapshots = memory.stats["snapshot_builds"]
        with measure(sm) as (counts, sql):
            refresh(world, sid, expired)
        samples.append(
            (
                counts["sql_selects"],
                counts["sql_rows"],
                memory.stats["parsed_builds"] - parsed,
                memory.stats["snapshot_builds"] - snapshots,
            )
        )
        effect_reads = [s for s in sql if "FROM ongoing_effects" in s]
        assert len(effect_reads) <= 1 and all("AND target_id=?" in s for s in effect_reads), sql
        assert sum("SELECT json_extract(state_json,?) FROM combats" in s for s in sql) == 1, sql

    monkeypatch.setattr(effect_time, "refresh_world_walk_expiry", observed_refresh)
    before = sm.read_combat_snapshot(sid, package, context=context)
    time_before = sm.read_time_sync(sid, package, context=context)
    facts = authority(sm, sid), effect_rows(sm, sid)
    sm._conn.execute(
        "CREATE TEMP TRIGGER fail_expiry_receipt BEFORE INSERT ON command_receipts "
        f"WHEN NEW.command_id='{command.command_id}' "
        "AND (SELECT count(*) FROM ongoing_effects WHERE session_id=NEW.session_id)=1 "
        "AND EXISTS (SELECT 1 FROM mechanical_actors WHERE session_id=NEW.session_id "
        f"AND actor_id='{target}' AND json_extract(state_json,'$.movement_remaining')=20) "
        "BEGIN SELECT RAISE(ABORT,'injected after world expiry and movement refresh'); END"
    )
    with pytest.raises(StateMachineError) as failure:
        sm.submit_command(command, package, context=context)
    assert failure.value.code == "storage_error"
    assert "injected after world expiry and movement refresh" in str(failure.value.__cause__)
    assert (authority(sm, sid), effect_rows(sm, sid)) == facts
    assert sm.read_combat_snapshot(sid, package, context=context) == before
    assert sm.read_time_sync(sid, package, context=context) == time_before
    assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
    sm._conn.execute("DROP TRIGGER fail_expiry_receipt")
    grow_scenes(sm, sid, 100)

    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "committed", receipt
    assert sm.submit_command(command, package, context=context) == receipt
    assert len(samples) == 2 and samples[0] == samples[1]
    assert samples[0][3] == 0
    print(
        "expiry maintenance at +0 / +100 scenes (selects, rows, parsed builds, snapshots):", samples
    )
    return receipt


@pytest.mark.parametrize("world_first", [False, True], ids=["ray-start-first", "world-clock-first"])
def test_longstrider_ray_overlap(timed_spell_world, world_first, monkeypatch):
    """Compose one target's walking budget and independently settle both effect clocks."""
    session = timed_spell_world(seed=16, walking=True, hit_rider=RAY, target_first=world_first)
    sm, sid, package, context = session

    def execute(command):
        """Submit the typed command through the public handler."""
        return sm.submit_command(command, package, context=context)

    def read():
        """Read the published combat snapshot."""
        return sm.read_combat_snapshot(sid, package, context=context)

    def identity(effect):
        """Use the stored composite identity shared by the two expiry paths."""
        return effect.target_id, effect.id, effect.origin

    def movement(snapshot):
        """Report remaining feet and previously spent movement on the shared ledger."""
        ledger = snapshot.combat_state.movement_ledgers[NPC]
        return actor(snapshot, NPC).movement_remaining, ledger.spent_ft, ledger.distance_ft

    def pass_turn(key, expected_actor):
        """Advance the expected actor's turn using the public Pass command."""
        command = pass_command(sid, read(), key)
        assert command.actor_id == expected_actor
        receipt = execute(command)
        assert receipt.status == "committed", receipt
        return receipt

    def walk(cell, key):
        """Issue ordinary walking for the current actor."""
        command = move_command(session, cell, key)
        assert command.actor_id == NPC
        return execute(command)

    def events_for(receipt):
        """Read the committed events belonging to this receipt."""
        return [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs
        ]

    def check_ray_expiry(receipt, ray):
        """Check exact Ray removal at the caster START on the closing Pass."""
        engine_events = [
            event.payload["engine_event"]
            for event in events_for(receipt)
            if event.event_type == "combat.engine_event"
        ]
        (index,) = [i for i, e in enumerate(engine_events) if e["type"] == "effect_expired"]
        expiry = engine_events[index]
        assert (expiry["target_id"], expiry["effect_id"], expiry["origin"]) == identity(ray)
        start = engine_events[index - 1]
        assert (start["type"], start["phase"], start["actor_id"]) == (
            "turn_phase",
            "turn_start",
            caster,
        )
        assert not any(event.event_type == "effect.expired" for event in events_for(receipt))

    def check_world_expiry(receipt, longstrider):
        """Check exact Longstrider removal at tick 3600 on the closing Pass."""
        (expiry,) = [e for e in events_for(receipt) if e.event_type == "effect.expired"]
        assert tuple(expiry.payload["identity"]) == identity(longstrider)
        assert expiry.payload["boundary_tick"] == 3600
        assert not any(
            event.payload["engine_event"]["type"] == "effect_expired"
            for event in events_for(receipt)
            if event.event_type == "combat.engine_event"
        )

    with sm:
        cast = world_cast_command(session, target_id=NPC)
        caster = cast.actor_id
        assert (receipt := execute(cast)).status == "committed", receipt
        for start, end in (("0,0", "0,1"), ("0,1", "0,2")):
            step = submit_world(sm, sid, package, context, StepActor(A, start, end), f"seek.{end}")
            assert step.status == "committed", step
        tick = 3594 if world_first else 3588
        assert (receipt := submit(session, AdvanceSessionTime(tick), "before-combat")).status == (
            "committed"
        ), receipt
        assert (receipt := initialize(sm, sid, package, context)).status == "committed", receipt
        (longstrider,) = read().effect_states
        assert identity(longstrider) == (NPC, "effect:longstrider", f"cast:longstrider:{caster}")
        assert movement(read()) == (40, 0, 0)

        if world_first:
            assert (receipt := walk("2,0", "longstrider.walk-five")).status == "committed", receipt
            pass_turn("target.to.caster", NPC)
            assert sm.read_time_sync(sid, package, context=context).committed_tick == 3594

        before_ray = read()
        assert (hit := execute(cast_command(sm, sid, package, context, RAY))).status == (
            "committed"
        ), hit
        both = read()
        assert both.effect_states[0] == longstrider
        ray = both.effect_states[1]
        (clock,) = both.combat_state.effect_lifecycles
        assert len(both.effect_states) == 2
        assert clock.identity == identity(ray) and ray.target_id == NPC
        assert clock.state.expiry_actor_id == caster and clock.state.application.source_slug == RAY
        hit_events = [
            event.payload["engine_event"]
            for event in events_for(hit)
            if event.event_type == "combat.engine_event"
        ]
        (roll,) = [event for event in hit_events if event["type"] == "attack_rolled"]
        (damage,) = [event for event in hit_events if event["type"] == "damage_applied"]
        rng = Random(16)
        natural = rng.randint(1, 20)
        cold_damage = rng.randint(1, 8) + rng.randint(1, 8)
        assert roll["is_hit"] and not roll["is_crit"] and roll["advantage"] == "normal"
        assert (roll["natural"], roll["modifier"], roll["roll_total"]) == (natural, 7, natural + 7)
        assert (damage["amount"], damage["damage_type"]) == (cold_damage, "cold")
        assert actor(both, NPC).hp_current == actor(before_ray, NPC).hp_current - cold_damage > 0
        assert actor(both, NPC).base_speed == 30
        assert both.resource_state == before_ray.resource_state
        assert {row[1:4] for row in effect_rows(sm, sid)[0]} == {
            identity(longstrider),
            identity(ray),
        }

        # Casting spends the Action but leaves the caster's Turn open.
        pass_turn("caster.after.ray", caster)
        after_caster_pass = read()

        if world_first:
            assert movement(both) == (25, 5, 5)
            assert sm.read_time_sync(sid, package, context=context).committed_tick == 3594
            boundary_command = pass_command(sid, read(), "third.expires.longstrider")
            assert boundary_command.actor_id == OTHER_ENEMY
            boundary = retry_clock_expiry_after_growth(
                sm, boundary_command, package, context, monkeypatch
            )
            check_world_expiry(boundary, longstrider)
            reduced = read()
            assert sm.read_time_sync(sid, package, context=context).committed_tick == 3600
            assert reduced.effect_states == (ray,)
            assert reduced.combat_state.effect_lifecycles == (clock,)
            assert tuple(row[1:4] for row in effect_rows(sm, sid)[0]) == (identity(ray),)
            assert movement(reduced) == (20, 0, 0)
            too_far = walk("7,0", "ray.walk-twenty-five")
            assert (too_far.status, too_far.error_code) == ("rejected", "movement.insufficient")
            assert read() == reduced
            assert (receipt := walk("6,0", "ray.walk-twenty")).status == "committed", receipt
            check_ray_expiry(pass_turn("target.expires.ray", NPC), ray)
            assert movement(read()) == (10, 20, 20)
            pass_turn("caster.to.third", caster)
            pass_turn("third.to.target", OTHER_ENEMY)
        else:
            assert movement(both) == (30, 0, 0)
            too_far = walk("8,0", "both.walk-thirty-five")
            assert (too_far.status, too_far.error_code) == ("rejected", "movement.insufficient")
            assert read() == after_caster_pass
            assert (receipt := walk("7,0", "both.walk-thirty")).status == "committed", receipt
            assert movement(read()) == (0, 30, 30)
            pass_turn("target.to.third", NPC)
            assert read().effect_states == (longstrider, ray)
            check_ray_expiry(pass_turn("third.expires.ray", OTHER_ENEMY), ray)
            assert read().effect_states == (longstrider,)
            assert not read().combat_state.effect_lifecycles
            assert sm.read_time_sync(sid, package, context=context).committed_tick == 3594
            assert movement(read()) == (10, 30, 30)
            assert tuple(row[1:4] for row in effect_rows(sm, sid)[0]) == (identity(longstrider),)
            pass_turn("caster.to.target", caster)
            assert movement(read()) == (40, 0, 0)
            assert (receipt := walk("0,0", "longstrider.walk-thirty-five")).status == (
                "committed"
            ), receipt
            assert movement(read()) == (5, 35, 35)
            pass_turn("target.to.third.again", NPC)
            check_world_expiry(pass_turn("third.expires.longstrider", OTHER_ENEMY), longstrider)
            assert sm.read_time_sync(sid, package, context=context).committed_tick == 3600
            assert movement(read()) == (0, 35, 35)
            pass_turn("caster.to.target.again", caster)

        final = read()
        assert movement(final) == (30, 0, 0)
        assert not final.effect_states and not final.combat_state.effect_lifecycles
        assert effect_rows(sm, sid) == ((), ())


@pytest.mark.parametrize("other_enemy_turn", [False, True], ids=["target-next", "target-later"])
def test_hit_slow_walk_and_expiry_at_casters_start(
    open_spell_combat, context, monkeypatch, other_enemy_turn
):
    """Link a persisted hit effect to ordinary movement and the next caster START."""
    sm, sid, package, loader, db = open_spell_combat(
        temp_hp=0, extra_spells=(RAY,), walking=True, other_enemy_turn=other_enemy_turn
    )
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        observed = observe_evaluation(sm, monkeypatch)
        before = sm.read_combat_snapshot(sid, package, context=context)
        original_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        result = observed[-1]
        assert_committed_events(sm, sid, package, context, receipt, result)
        hit = sm.read_combat_snapshot(sid, package, context=context)
        (effect,) = hit.effect_states
        (clock,) = hit.combat_state.effect_lifecycles
        (lineage,) = hit.combat_state.conditions_by_effect
        (creation,) = [
            op for op in result.state_delta.operations if type(op) is delta.CombatEffectCreate
        ]
        (damage,) = [event for event in result.proposed_events if event.type == "damage_applied"]
        state = hit.combat_state
        assert state.initiative_ids[state.current_turn_index] == command.actor_id
        assert effect == creation.effect and effect.target_id == NPC
        assert clock == creation.lifecycle and lineage == creation.lineage and not lineage.statuses
        assert clock.state.expiry_actor_id == command.actor_id
        assert clock.state.applied_turn_serial == before.combat_state.turn_serial
        assert clock.state.application.source_slug == RAY
        assert clock.state.application.spec.expiry_boundary == "source_next_turn_start"
        assert actor(hit, NPC).hp_current == actor(before, NPC).hp_current - damage.amount
        assert damage.damage_type == "cold"
        assert actor(hit, NPC).movement_remaining == actor(hit, NPC).base_speed - 10 == 20
        assert (
            actor(hit, command.actor_id).spell_slots == actor(before, command.actor_id).spell_slots
        )
        assert state.expended_resources == before.combat_state.expended_resources
        assert sm.read_rng_context(sid, package, context=context) != original_rng
        assert sm.submit_command(command, package, context=context) == receipt
        assert effect_rows(sm, sid)[0][0][1:4] == creation.identity
        assert effect_rows(sm, sid)[1] == ()
        assert not before.effect_states and actor(before, NPC).hp_current == 40
        with StateMachine(db, dnd_loader=loader) as reopened:
            assert reopened.read_combat_snapshot(sid, package, context=context) == hit

        caster_pass = pass_command(sid, hit, "ray.caster-after-cast")
        assert caster_pass.actor_id == command.actor_id
        assert sm.submit_command(caster_pass, package, context=context).status == "committed"
        hit = sm.read_combat_snapshot(sid, package, context=context)
        assert hit.combat_state.initiative_ids[hit.combat_state.current_turn_index] == (
            OTHER_ENEMY if other_enemy_turn else NPC
        )

        if other_enemy_turn:
            earlier = pass_command(sid, hit, "ray.before-target")
            receipt = sm.submit_command(earlier, package, context=context)
            assert receipt.status == "committed", receipt
            assert earlier.actor_id == OTHER_ENEMY
            assert not any(e.type == "effect_expired" for e in observed[-1].proposed_events)
            hit = sm.read_combat_snapshot(sid, package, context=context)
            assert hit.effect_states == (effect,) and hit.combat_state.effect_lifecycles == (clock,)
            assert actor(hit, NPC).movement_remaining == 20

        movement = sm, sid, package, context
        longer = move_command(movement, "11,0", "ray.walk-25")
        assert longer.actor_id == NPC
        refused = sm.submit_command(longer, package, context=context)
        assert (refused.status, refused.error_code) == ("rejected", "movement.insufficient")
        assert sm.read_combat_snapshot(sid, package, context=context) == hit
        walk_command = move_command(movement, "10,0", "ray.walk-20")
        assert walk_command.actor_id == NPC
        walk = sm.submit_command(walk_command, package, context=context)
        assert walk.status == "committed", walk
        moved = sm.read_combat_snapshot(sid, package, context=context)
        assert moved.combat_state.actor_zone[NPC] == "10,0"
        assert moved.combat_state.movement_ledgers[NPC].spent_ft == 20
        assert actor(moved, NPC).movement_remaining == 0
        assert moved.effect_states == (effect,)

        end_target = pass_command(sid, moved, "ray.caster-next-start")
        receipt = sm.submit_command(end_target, package, context=context)
        assert receipt.status == "committed", receipt
        expired = sm.read_combat_snapshot(sid, package, context=context)
        assert effect_rows(sm, sid) == ((), ())
        assert not expired.effect_states
        assert not expired.combat_state.effect_lifecycles
        assert not expired.combat_state.conditions_by_effect
        assert expired.combat_state.turn_serial == clock.state.applied_turn_serial + 2 + int(
            other_enemy_turn
        )
        assert_committed_events(sm, sid, package, context, receipt, observed[-1])
        events = observed[-1].proposed_events
        (index,) = [i for i, event in enumerate(events) if event.type == "effect_expired"]
        assert events[1].type == "turn_phase" and events[1].phase == "turn_end"
        assert events[1].actor_id == NPC
        assert events[index - 2].type == "turn_started"
        assert events[index - 2].actor_id == command.actor_id
        assert events[index - 1].type == "turn_phase" and events[index - 1].phase == "turn_start"
        assert events[index - 1].actor_id == command.actor_id
        assert events[index].reason == "duration" and events[index].origin == effect.origin
        assert moved.effect_states == (effect,) and moved.combat_state.effect_lifecycles == (clock,)

        reset = expired
        for i in range(1 + int(other_enemy_turn)):
            receipt = sm.submit_command(
                pass_command(sid, reset, f"ray.target-next-start-{i}"), package, context=context
            )
            assert receipt.status == "committed", receipt
            reset = sm.read_combat_snapshot(sid, package, context=context)
        assert actor(reset, NPC).movement_remaining == 30
        restored = sm.submit_command(
            move_command(movement, "15,0", "ray.walk-25-restored"), package, context=context
        )
        assert restored.status == "committed", restored


def test_miss_commits_cast_without_effect_or_damage(open_spell_combat, context, monkeypatch):
    """A real miss spends the Action and roll with no effect or target HP edit."""
    sm, sid, package, _, _ = open_spell_combat(seed=31, temp_hp=0, extra_spells=(RAY,))
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        observed = observe_evaluation(sm, monkeypatch)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        result = observed[-1]
        (roll,) = [event for event in result.proposed_events if event.type == "attack_rolled"]
        assert not roll.is_hit
        assert not any(
            e.type in {"effect_applied", "damage_applied"} for e in result.proposed_events
        )
        assert_committed_events(sm, sid, package, context, receipt, result)
        assert effect_rows(sm, sid) == ((), ())
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert not after.effect_states
        assert not after.combat_state.effect_lifecycles
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current
        assert actor(after, NPC).movement_remaining == 30
        assert actor(after, command.actor_id).action_taken_this_turn
        assert sm.read_rng_context(sid, package, context=context) != rng


def test_speed_effect_with_unrelated_overlap_stays_unsupported(open_spell_combat, context):
    """An existing speed effect admits only the graph that Pass and walk can adopt."""
    sm, sid, package, _, _ = open_spell_combat(temp_hp=0, extra_spells=(RAY,), walking=True)
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        assert sm.submit_command(command, package, context=context).status == "committed"
        hit = sm.read_combat_snapshot(sid, package, context=context)
        (effect,) = hit.effect_states
        unrelated = effect.model_dump(mode="json")
        unrelated.update(id="effect:unrelated", origin="fixture", lifecycle=None, changes=[])
        with sm._store.transaction(write=True):
            sm._characters.effects.add_effect(sid, unrelated)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        stored = effect_rows(sm, sid)
        assert len(before.effect_states) == 2
        inputs = (
            (pass_command(sid, before, "ray.overlap-pass"), "turn.capability"),
            (
                move_command((sm, sid, package, context), "7,0", "ray.overlap-walk"),
                "movement.capability",
            ),
        )
        for command, code in inputs:
            receipt = sm.submit_command(command, package, context=context)
            assert (receipt.status, receipt.error_code) == ("rejected", code), receipt
            assert receipt.event_seqs == ()
            assert sm.read_combat_snapshot(sid, package, context=context) == before
            assert sm.read_rng_context(sid, package, context=context) == rng
            assert effect_rows(sm, sid) == stored


def test_nonfinal_lethal_rider_requires_supported_pass(open_spell_combat, context, monkeypatch):
    """Reject an accepted RE hit whose clock could not advance by public Pass."""
    sm, sid, package, _, _ = open_spell_combat(
        hp=1, temp_hp=0, extra_spells=(RAY,), walking=True, other_enemy_turn=True
    )
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        observed = observe_evaluation(sm, monkeypatch)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "spell.capability")
        assert receipt.event_seqs == ()
        assert observed[-1].status == "accepted"
        assert any(
            type(op) is delta.CombatEffectCreate for op in observed[-1].state_delta.operations
        )
        assert any(event.type == "death" for event in observed[-1].proposed_events)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert effect_rows(sm, sid) == ((), ())
        assert sm.submit_command(command, package, context=context) == receipt


@pytest.mark.parametrize("removal", [False, True], ids=["create", "expire"])
def test_hit_effect_rolls_back_at_receipt(open_spell_combat, context, removal):
    """Inject a real SQL failure after the effect and related facts were written."""
    sm, sid, package, _, _ = open_spell_combat(temp_hp=0, extra_spells=(RAY,))
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        if removal:
            assert sm.submit_command(command, package, context=context).status == "committed"
            snapshot = sm.read_combat_snapshot(sid, package, context=context)
            caster_pass = pass_command(sid, snapshot, "ray.rollback-caster-pass")
            assert caster_pass.actor_id == command.actor_id
            assert sm.submit_command(caster_pass, package, context=context).status == "committed"
            snapshot = sm.read_combat_snapshot(sid, package, context=context)
            command = pass_command(sid, snapshot, "ray.rollback-expire")
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        stored = authority(sm, sid), effect_rows(sm, sid)
        condition = "NOT EXISTS" if removal else "EXISTS"
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_speed_receipt BEFORE INSERT ON command_receipts "
            f"WHEN NEW.command_id='{command.command_id}' AND {condition} "
            "(SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id) "
            "BEGIN SELECT RAISE(ABORT,'injected after speed effect write'); END"
        )
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(command, package, context=context)
        assert failure.value.code == "storage_error"
        assert "injected after speed effect write" in str(failure.value.__cause__)
        assert (authority(sm, sid), effect_rows(sm, sid)) == stored
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_speed_receipt")
        assert sm.submit_command(command, package, context=context).status == "committed"
        assert bool(effect_rows(sm, sid)[0]) is not removal


@pytest.mark.parametrize("omit", ["effect_applied", "combat.effect_create"])
def test_cast_requires_complete_hit_effect_evidence(open_spell_combat, context, monkeypatch, omit):
    """A distorted accepted result discards the whole hit and consumes no RNG."""
    sm, sid, package, _, _ = open_spell_combat(temp_hp=0, extra_spells=(RAY,))
    with sm:
        command = replace(cast_command(sm, sid, package, context, RAY), command_id="ray.forge")
        before = sm.read_combat_snapshot(sid, package, context=context)
        stored = authority(sm, sid), effect_rows(sm, sid)
        evaluate = sm._rules._evaluate_dnd

        async def incomplete(request):
            """Drop just one side of the real result's effect evidence."""
            result = await evaluate(request)
            return result.model_copy(
                update={
                    "proposed_events": tuple(e for e in result.proposed_events if e.type != omit),
                    "state_delta": result.state_delta.model_copy(
                        update={
                            "operations": tuple(
                                op for op in result.state_delta.operations if op.kind != omit
                            )
                        }
                    ),
                }
            )

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", incomplete)
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(command, package, context=context)
        assert failure.value.code == "invalid_rule_result"
        assert (authority(sm, sid), effect_rows(sm, sid)) == stored
        assert sm.read_combat_snapshot(sid, package, context=context) == before
