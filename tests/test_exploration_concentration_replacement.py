"""Public exploration recasts, exact concentration members, and rollback."""

import pytest
from dnd5e_engine.spatial import GridTopology
from item_support import NPC
from spatial_support import submit as submit_world
from test_session_time_coordination import submit as submit_time
from test_timed_spell_cast import PERCEPTION_BONUS, PERCEPTION_SPELL, cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_magic_light import ITEM, cast, sources
from test_world_magic_light import magic_world as magic_world

from trpg_state_machine import (
    AdvanceSessionTime,
    DropWorldConcentration,
    StateMachine,
    StateMachineError,
)


@pytest.fixture
def concentrating_world(timed_spell_world):
    """Bind an authored one-minute Perception spell and published Darkness."""

    def configure(data):
        for scene in data["scenes"]:
            scene["grid"]["width"] = 14
        for foundation in data["rule_bindings"].values():
            foundation["snapshot"]["scene_state"]["grid"]["width"] = 14
            for scene in foundation.get("additional_scenes", ()):
                scene["grid"]["width"] = 14

    return timed_spell_world(
        spell_id=PERCEPTION_SPELL,
        additional_spells=("darkness", "longstrider"),
        configure=configure,
        configure_spell=lambda spell: spell.model_copy(update={"concentration": True}),
    )


def test_darkness_recast_replaces_the_exact_region_and_is_retryable(magic_world):
    sm, sid, package, context = magic_world
    with sm:
        _, first = cast(
            magic_world, "first-darkness", "darkness", 2, {"kind": "point", "cell_id": "2,0"}
        )
        assert first.status == "committed", first
        (original,) = sources(magic_world)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        before_events = sm.read_events(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        old_grid = GridTopology(before.scene_state.grid)
        assert old_grid.magical_darkness_on_cell("2,0")
        assert not old_grid.magical_darkness_on_cell("12,0")

        _, rejected = cast(
            magic_world, "invalid-darkness", "darkness", 2, {"kind": "item", "item_id": ITEM}
        )
        assert (rejected.status, rejected.error_code) == ("rejected", "target_invalid"), rejected
        assert rejected.event_seqs == ()
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before

        # Fail only after both the original removal and the successor insert occurred.
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_concentration_recast BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='second-darkness' "
            "AND NOT EXISTS (SELECT 1 FROM effect_concentrations WHERE session_id=NEW.session_id "
            "AND effect_id='spatial:first-darkness') "
            "AND EXISTS (SELECT 1 FROM effect_concentrations WHERE session_id=NEW.session_id "
            "AND effect_id='spatial:second-darkness') "
            "BEGIN SELECT RAISE(ABORT,'injected concentration recast failure'); END"
        )
        destination = {"kind": "point", "cell_id": "12,0"}
        with pytest.raises(StateMachineError):
            cast(magic_world, "second-darkness", "darkness", 2, destination)
        assert sm.get_command_receipt(sid, "second-darkness", package, context=context) is None
        assert sources(magic_world) == (original,)
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_events(sid, package, context=context) == before_events
        assert sm.read_rng_context(sid, package, context=context) == before_rng

        sm._conn.execute("DROP TRIGGER fail_concentration_recast")
        command, receipt = cast(magic_world, "second-darkness", "darkness", 2, destination)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(command, package, context=context) == receipt
        (successor,) = sources(magic_world)
        assert successor.effect_id == "spatial:second-darkness"
        assert successor.concentration_identity != original.concentration_identity
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        new_grid = GridTopology(after.scene_state.grid)
        assert old_grid.magical_darkness_on_cell("2,0")
        assert not new_grid.magical_darkness_on_cell("2,0")
        assert new_grid.magical_darkness_on_cell("12,0")
        old_slot = next(pool for pool in before.resource_state.pools if pool.pool_id == "spell:2")
        new_slot = next(pool for pool in after.resource_state.pools if pool.pool_id == "spell:2")
        assert new_slot.current == old_slot.current - 1
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        (ended,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "effect.spatial_light_ended"
        ]
        assert ended.payload["effect_id"] == original.effect_id
        assert ended.payload["reason"] == "concentration_replaced"
        assert ended.event_seq in receipt.event_seqs


def test_public_casts_replace_spatial_and_actor_concentration_atomically(concentrating_world):
    sm, sid, package, context = concentrating_world

    def snapshot():
        return sm.read_noncombat_snapshot(sid, package, context=context)

    with sm:
        companion = cast_command(concentrating_world, target_id=NPC)
        assert sm.submit_command(companion, package, context=context).status == "committed"
        (independent,) = snapshot().effect_states
        _, initial = cast(
            concentrating_world, "old-darkness", "darkness", 2, {"kind": "point", "cell_id": "8,0"}
        )
        assert initial.status == "committed", initial
        (darkness,) = sources(concentrating_world)
        before_actor = snapshot()
        before_rng = sm.read_rng_context(sid, package, context=context)
        rejected_actor_cast = cast_command(
            concentrating_world,
            spell_id=PERCEPTION_SPELL,
            slot_level=2,
            target_id=NPC,
            command_id="wrong-actor-slot",
        )
        refused = sm.submit_command(rejected_actor_cast, package, context=context)
        assert (refused.status, refused.error_code) == ("rejected", "spell.slot"), refused
        assert snapshot() == before_actor
        assert sources(concentrating_world) == (darkness,)
        actor_cast = cast_command(
            concentrating_world, spell_id=PERCEPTION_SPELL, target_id=NPC, command_id="actor-group"
        )
        accepted = sm.submit_command(actor_cast, package, context=context)
        assert accepted.status == "committed", accepted
        assert sm.submit_command(actor_cast, package, context=context) == accepted
        assert sources(concentrating_world) == ()
        assert GridTopology(before_actor.scene_state.grid).magical_darkness_on_cell("8,0")
        active = snapshot()
        assert not GridTopology(active.scene_state.grid).magical_darkness_on_cell("8,0")
        assert independent in active.effect_states
        (member,) = [effect for effect in active.effect_states if effect != independent]
        identity = (member.target_id, member.id, member.origin)
        assert identity[0] == NPC
        assert member.duration.seconds == 60
        assert member.changes[0].key == PERCEPTION_BONUS
        caster = next(
            actor for actor in active.character_states if actor.entity_id == actor_cast.actor_id
        )
        assert caster.concentration_effect_id == member.id
        old_slots = {pool.pool_id: pool.current for pool in before_actor.resource_state.pools}
        active_slots = {pool.pool_id: pool.current for pool in active.resource_state.pools}
        assert active_slots == {**old_slots, "spell:1": old_slots["spell:1"] - 1}
        (spatial_end,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in accepted.event_seqs
            and event.event_type == "effect.spatial_light_ended"
        ]
        assert spatial_end.payload["effect_id"] == darkness.effect_id
        assert spatial_end.payload["reason"] == "concentration_replaced"

        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_mixed_concentration BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='new-darkness' "
            "AND NOT EXISTS (SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id "
            "AND concentration_id='actor:actor-group') "
            "AND EXISTS (SELECT 1 FROM effect_concentrations WHERE session_id=NEW.session_id "
            "AND effect_id='spatial:new-darkness') "
            "BEGIN SELECT RAISE(ABORT,'injected mixed concentration failure'); END"
        )
        before_events = sm.read_events(sid, package, context=context)
        point = {"kind": "point", "cell_id": "2,0"}
        with pytest.raises(StateMachineError):
            cast(concentrating_world, "new-darkness", "darkness", 2, point)
        assert sm.get_command_receipt(sid, "new-darkness", package, context=context) is None
        assert snapshot() == active
        assert sources(concentrating_world) == ()
        assert sm.read_events(sid, package, context=context) == before_events

        sm._conn.execute("DROP TRIGGER fail_mixed_concentration")
        replacement, receipt = cast(concentrating_world, "new-darkness", "darkness", 2, point)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(replacement, package, context=context) == receipt
        (new_darkness,) = sources(concentrating_world)
        assert new_darkness.effect_id == "spatial:new-darkness"
        after = snapshot()
        assert after.effect_states == (independent,)
        assert active.effect_states == (independent, member)
        assert {pool.pool_id: pool.current for pool in after.resource_state.pools} == {
            **active_slots,
            "spell:2": active_slots["spell:2"] - 1,
        }
        (ended,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs
            and event.event_type == "effect.concentration_ended"
        ]
        assert tuple(ended.payload["identity"]) == identity
        assert ended.payload["concentration_id"] == "actor:actor-group"
        assert ended.payload["reason"] == "concentration_replaced"
        assert sm.read_rng_context(sid, package, context=context) == before_rng


def test_world_clock_and_drop_release_actor_concentration(concentrating_world, tmp_path):
    sm, sid, package, context = concentrating_world

    def apply(command_id):
        command = cast_command(
            concentrating_world,
            spell_id=PERCEPTION_SPELL,
            command_id=command_id,
        )
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt

    def effects():
        return sm.read_noncombat_snapshot(sid, package, context=context).effect_states

    with sm:
        apply("first-actor")
        (original,) = effects()
        assert (
            submit_time(concentrating_world, AdvanceSessionTime(30), "before-recast").status
            == "committed"
        )
        apply("recast-actor")
        assert (
            submit_time(concentrating_world, AdvanceSessionTime(60), "original-expiry").status
            == "committed"
        )
        assert effects() == (original,)
        saved = sm.read_noncombat_snapshot(sid, package, context=context)
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as sm:
        concentrating_world = (sm, sid, package, context)
        assert sm.read_noncombat_snapshot(sid, package, context=context) == saved
        assert (
            submit_time(concentrating_world, AdvanceSessionTime(90), "new-expiry").status
            == "committed"
        )
        assert effects() == ()

        apply("voluntary-actor")
        receipt = submit_world(sm, sid, package, context, DropWorldConcentration(), "drop-actor")
        assert receipt.status == "committed", receipt
        assert effects() == ()
