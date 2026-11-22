"""Exercise exact early endings through public world commands and reopen."""

from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_world_effect import EffectExpiryPolicy, WorldEffectApplication
from item_support import NPC
from spatial_support import command as world_command
from test_ongoing_effect_storage import _effect
from test_scoped_runtime_validation import measure, observe
from test_storage_conditions import _condition
from test_timed_spell_cast import cast_command, first_level_balance
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_magic_light import ITEM, _magic_world, prepare_cast, sources

from trpg_state_machine import (
    DismissWorldSpell,
    StateMachine,
    StateMachineError,
    world_command_from_data,
)


def test_cast_light_ends_invisibility_in_same_retriable_commit(
    timed_spell_world, tmp_path, monkeypatch
):
    session = _magic_world(timed_spell_world, additional_spells=("invisibility",))
    sm, sid, package, context = session
    loader = sm._world.loader

    def snapshot(state_machine):
        return state_machine.read_noncombat_snapshot(sid, package, context=context)

    with sm:
        invisible = cast_command(session, spell_id="invisibility", slot_level=2, command_id="hide")
        receipt = sm.submit_command(invisible, package, context=context)
        assert receipt.status == "committed", receipt
        hidden = snapshot(sm)
        caster = next(a for a in hidden.character_states if a.entity_id == invisible.actor_id)
        assert [condition.condition for condition in caster.conditions] == ["invisible"]
        assert caster.concentration_effect_id == "effect:invisibility"
        (application,) = hidden.effect_states
        identity = (application.target_id, application.id, application.origin)
        rejected = prepare_cast(
            session, "invalid-light", "light", 0, {"kind": "item", "item_id": "missing"}
        )
        refusal = sm.submit_command(rejected, package, context=context)
        assert refusal.status == "rejected", refusal
        assert snapshot(sm) == hidden
        assert sources(session) == ()

        light = prepare_cast(session, "show-light", "light", 0, {"kind": "item", "item_id": ITEM})
        rng = sm.read_rng_context(sid, package, context=context)

        def abort(*_):
            assert sm._world.characters.effects.spell_application(sid, identity) is None
            raise RuntimeError("rollback after exact spell end and Light creation")

        with monkeypatch.context() as patch:
            patch.setattr(sm._world.magic, "reconcile_scene", abort)
            with pytest.raises(RuntimeError, match="rollback after exact spell end"):
                sm.submit_command(light, package, context=context)

        assert sources(session) == ()
        assert snapshot(sm) == hidden
        assert sm.read_rng_context(sid, package, context=context) == rng
        committed = sm.submit_command(light, package, context=context)
        assert committed.status == "committed", committed
        current = snapshot(sm)
        caster = next(a for a in current.character_states if a.entity_id == invisible.actor_id)
        assert current.effect_states == () and caster.conditions == []
        assert caster.concentration_effect_id is None
        assert [source.effect_id for source in sources(session)] == ["spatial:show-light"]
        assert sm.read_rng_context(sid, package, context=context) == rng
        events = tuple(
            event
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == light.command_id
        )
        assert tuple(event.event_seq for event in events) == committed.event_seqs
        (ending,) = [event for event in events if event.event_type == "effect.spell_ended"]
        cast = next(
            event
            for event in events
            if event.payload.get("engine_event", {}).get("type") == "spell_cast"
        )
        assert cast.event_seq < ending.event_seq
        assert tuple(ending.payload["identity"]) == identity
        assert ending.payload["reason"] == "spell_cast"
        assert ending.payload["cast_command_id"] == invisible.command_id
        assert sm.submit_command(light, package, context=context) == committed

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        assert snapshot(reopened) == current
        assert reopened.submit_command(light, package, context=context) == committed
        assert [source.effect_id for source in sources((reopened, sid, package, context))] == [
            "spatial:show-light"
        ]


def test_dismiss_longstrider_selects_one_original_cast_without_payment(timed_spell_world):
    session = timed_spell_world()
    sm, sid, package, context = session
    with sm:
        first = cast_command(session, command_id="first-walk")
        assert sm.submit_command(first, package, context=context).status == "committed"
        second = cast_command(session, command_id="second-walk")
        assert sm.submit_command(second, package, context=context).status == "committed"
        effect_time = sm._session_runtime.time.consumers["effects"]
        with sm._transaction(write=True):
            other = effect_time.apply_longstrider(sid, package, "other-caster", NPC, first.actor_id)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        active, surviving, independent = before.effect_states
        assert independent == other.effect
        foreign = world_command(
            sm,
            sid,
            package,
            context,
            DismissWorldSpell(*other.identity, other.command_id),
            "foreign",
        )
        refused = sm.submit_command(foreign, package, context=context)
        assert (refused.status, refused.error_code) == ("rejected", "spell.dismiss")
        selection = DismissWorldSpell(active.target_id, active.id, active.origin, first.command_id)
        stale = world_command(
            sm,
            sid,
            package,
            context,
            replace(selection, cast_command_id="missing-cast"),
            "wrong-cast",
        )
        refused = sm.submit_command(stale, package, context=context)
        assert (refused.status, refused.error_code) == ("rejected", "spell.application")
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        with sm._transaction(write=True):
            sm._characters.replace_conditions(sid, first.actor_id, [_condition("incapacitated")])
        unable = world_command(sm, sid, package, context, selection, "incapacitated")
        refused = sm.submit_command(unable, package, context=context)
        assert (refused.status, refused.error_code) == ("rejected", "spell.dismiss")
        assert (
            sm.read_noncombat_snapshot(sid, package, context=context).effect_states
            == before.effect_states
        )
        with sm._transaction(write=True):
            sm._characters.replace_conditions(sid, first.actor_id, [])
        dismiss = world_command(sm, sid, package, context, selection, "dismiss-first")
        with pytest.raises(StateMachineError, match="selected PC"):
            sm.submit_command(replace(dismiss, actor_id="npc.attendant"), package, context=context)
        wire = asdict(dismiss)
        wire.pop("check_world_effects")
        parsed = world_command_from_data(wire)
        assert parsed == dismiss
        slots = first_level_balance(sm, first)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(parsed, package, context=context)
        assert receipt.status == "committed", receipt
        assert first_level_balance(sm, first) == slots
        assert sm.read_rng_context(sid, package, context=context) == rng
        current = sm.read_noncombat_snapshot(sid, package, context=context)
        assert current.effect_states == (surviving, independent)
        assert sm.submit_command(dismiss, package, context=context) == receipt


def test_spell_end_reads_stay_local_as_unrelated_effects_grow(timed_spell_world, monkeypatch):
    """Check the indexed request path with additional effects on an unrelated actor."""
    rows, calls = [], []
    for growth in (0, 96):
        session = _magic_world(
            timed_spell_world, additional_spells=("invisibility",), growth=growth
        )
        sm, sid, package, context = session
        with sm:
            with sm._transaction(write=True):
                for i in range(growth):
                    sm._characters.effects.add_effect(sid, _effect(NPC, f"unrelated-{i}"))
            invisible = cast_command(session, spell_id="invisibility", slot_level=2)
            assert sm.submit_command(invisible, package, context=context).status == "committed"
            light = prepare_cast(
                session, "show-light", "light", 0, {"kind": "item", "item_id": ITEM}
            )
            with measure(sm) as (counts, _), monkeypatch.context() as patch:
                observe(patch, sm._characters.effects, "_spell_application", counts, "projections")
                receipt = sm.submit_command(light, package, context=context)
            assert receipt.status == "committed", receipt
            rows.append(counts["sql_rows"])
            calls.append(counts["projections"])
            plan = sm._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM ongoing_effects "
                "WHERE session_id=? AND target_id=? "
                "AND effect_id=? AND cast_spell_id=?",
                (sid, invisible.actor_id, "effect:invisibility", "invisibility"),
            ).fetchone()[3]
            assert "SEARCH" in plan and "session_id=? AND target_id=? AND effect_id=?" in plan
    assert rows[0] == rows[1]
    assert calls == [2, 2]


def test_light_endings_follow_target_effect_order_across_sources(timed_spell_world):
    """Select two actual owner rows whose origins sort against application order."""
    session = _magic_world(timed_spell_world, additional_spells=("invisibility",))
    sm, sid, package, context = session
    with sm:
        first = cast_command(
            session, spell_id="invisibility", slot_level=2, command_id="first-hide"
        )
        assert sm.submit_command(first, package, context=context).status == "committed"
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        (effect,) = before.effect_states
        later = effect.model_copy(update={"origin": f"cast:invisibility:{NPC}"})
        # Exploration admission currently casts only from the selected PC.
        # Install the later caster's canonical application at the trusted owner.
        with sm._store.transaction(write=True):
            row = sm._world.sessions._session(sid, package, context)
            sm._session_runtime.time.consumers["effects"].apply_verified(
                sid,
                WorldEffectApplication(
                    session_id=sid,
                    command_id="later-hide",
                    source_id=NPC,
                    effect=later,
                    expiry=EffectExpiryPolicy(action="remove"),
                    concentration=True,
                    ruleset_binding=sm._rules.session_binding(row),
                ),
                spell_id="invisibility",
            )
        assert later.origin < effect.origin
        light = prepare_cast(session, "clear-both", "light", 0, {"kind": "item", "item_id": ITEM})
        receipt = sm.submit_command(light, package, context=context)
        assert receipt.status == "committed", receipt
        endings = [
            event.payload["cast_command_id"]
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == light.command_id
            and event.event_type == "effect.spell_ended"
        ]
        assert endings == [first.command_id, "later-hide"]
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()
