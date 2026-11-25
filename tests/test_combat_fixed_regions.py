"""Adopt opted-in fixed Poisoned coverage through public combat boundaries."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import initialize
from test_timed_spell_cast import NPC, A, move_command, pass_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    EnvironmentArea,
    EnvironmentConditions,
    EnvironmentSceneFacts,
    EnvironmentSource,
    InitializeEnvironment,
    StateMachine,
    StateMachineError,
    TypedCommand,
)


@pytest.mark.parametrize("unrelated_sources", [0, 32])
def test_fixed_region_enter_turn_and_exit_share_authoritative_effects(
    timed_spell_world, tmp_path, monkeypatch, unrelated_sources
):
    """Enter overlapping sources, apply a late first turn, and consume Poisoned on attack."""

    def arrange_initial_turn(data):
        """Keep the ogre's first real start after the hero has moved within reach."""
        combat = data["rule_bindings"]["combat_foundation"]["snapshot"]["combat_state"]
        combat["initiative_ids"].reverse()
        combat["actor_zone"][NPC] = "7,0"
        data["actors"][0]["initial_cell"] = "7,0"

    session = timed_spell_world(
        walking=True, attack_after_walk=True, seed=1, configure=arrange_initial_turn
    )
    sm, sid, package, context = session

    def read():
        """Read the published encounter snapshot."""
        return sm.read_combat_snapshot(sid, package, context=context)

    def submit(command):
        """Commit one public command and surface any capability refusal."""
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        return receipt

    def effects_for(snapshot, actor_id):
        """Collect exact active sources without reducing them to the Poisoned name."""
        return {effect.origin for effect in snapshot.effect_states if effect.target_id == actor_id}

    alpha = f'region:poisoned:["{A}","toxic.alpha"]'
    beta = f'region:poisoned:["{A}","toxic.beta"]'
    ogre = f'region:poisoned:["{A}","toxic.ogre"]'
    with sm:
        sources = tuple(
            EnvironmentSource(
                f"toxic.{name}",
                EnvironmentConditions(),
                EnvironmentArea(cells),
                region_condition="poisoned",
                combat_region="entry-and-turn-start",
            )
            for name, cells in (
                ("alpha", ("1,0", "2,0")),
                ("beta", ("2,0", "3,0", "4,0", "5,0", "6,0")),
                ("ogre", ("7,0",)),
                *((f"unrelated-{i}", ("11,2",)) for i in range(unrelated_sources)),
            )
        )
        seeded = sm.environment.initialize(
            sid,
            (InitializeEnvironment(A, EnvironmentSceneFacts(sources=sources)),),
            package,
            command_id="combat.region.seed",
            expected_world_version=sm.read_session(sid, package, context=context).world_version,
            context=replace(context, can_manage_environment=True),
        )
        assert seeded.status == "committed", seeded
        assert initialize(sm, sid, package, context).status == "committed"
        original = read()
        hero = original.combat_state.initiative_ids[original.combat_state.current_turn_index]
        assert hero != NPC
        assert not original.effect_states
        old_rng = sm.read_rng_context(sid, package, context=context)
        old_tick = sm.read_time_sync(sid, package, context=context).committed_tick

        enter = move_command(session, "1,0", "combat.region.enter")
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_region_after_effect BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='combat.region.enter' AND EXISTS "
            "(SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id) "
            "BEGIN SELECT RAISE(ABORT,'injected after area adoption'); END"
        )
        try:
            with pytest.raises(StateMachineError) as failure:
                sm.submit_command(enter, package, context=context)
            assert str(failure.value.__cause__) == "injected after area adoption"
            assert read() == original
            assert sm.read_rng_context(sid, package, context=context) == old_rng
            assert sm.get_command_receipt(sid, enter.command_id, package, context=context) is None
        finally:
            sm._conn.execute("DROP TRIGGER fail_region_after_effect")
        queried = []
        lookup = sm._world.environment.region_condition_sources

        def record_sources(session_id, scene, cell, *, combat=False):
            """Record the production call's actual queried cell and returned sources."""
            found = lookup(session_id, scene, cell, combat=combat)
            queried.append((scene, cell, combat, len(found)))
            return found

        with monkeypatch.context() as record:
            record.setattr(sm._world.environment, "region_condition_sources", record_sources)
            receipt = submit(enter)
            assert submit(enter) == receipt
        assert queried == [
            (A, "0,0", False, 0),
            (A, "0,0", True, 0),
            (A, "1,0", False, 1),
            (A, "1,0", True, 1),
        ]
        assert effects_for(read(), hero) == {alpha}
        submit(move_command(session, "2,0", "combat.region.overlap"))
        assert effects_for(read(), hero) == {alpha, beta}
        submit(move_command(session, "6,0", "combat.region.leave-alpha"))
        before_start = read()
        assert effects_for(before_start, hero) == {beta}
        assert not effects_for(before_start, NPC)
        submit(pass_command(sid, before_start, "combat.region.ogre-start"))
        started = read()
        assert effects_for(started, NPC) == {ogre}
        assert effects_for(started, hero) == {beta}
        assert started.combat_state.turn_serial == before_start.combat_state.turn_serial + 1
        assert sm.read_rng_context(sid, package, context=context) == old_rng
        assert sm.read_time_sync(sid, package, context=context).committed_tick == old_tick

        attack = submit(
            TypedCommand(
                sid,
                "combat.region.ogre-attack",
                NPC,
                "combat.intent",
                CombatIntentPayload(
                    intent_type="attack", target_id=hero, stat_block_action_id="greatclub"
                ),
                started.world_version,
            )
        )
        (roll,) = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in attack.event_seqs
            and event.payload.get("engine_event", {}).get("type") == "attack_rolled"
        ]
        assert roll["advantage"] == "disadvantage"
        assert original.effect_states == ()
        after_attack = read()
        assert effects_for(after_attack, hero) == {beta}
        submit(pass_command(sid, after_attack, "combat.region.hero-start"))
        submit(move_command(session, "6,1", "combat.region.leave-beta"))
        final = read()
        assert effects_for(final, NPC) == {ogre}
        assert not effects_for(final, hero)
        assert final.combat_state.actor_zone[hero] == "6,1"
        final_rng = sm.read_rng_context(sid, package, context=context)
        final_tick = sm.read_time_sync(sid, package, context=context).committed_tick
        assert final_tick == old_tick + 6
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == final
        assert reopened.read_rng_context(sid, package, context=context) == final_rng
        assert reopened.read_time_sync(sid, package, context=context).committed_tick == final_tick
