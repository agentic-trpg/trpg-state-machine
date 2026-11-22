"""Exercise real SRD Aid applications through the public exploration command."""

from dataclasses import asdict

import pytest
from spatial_support import command as world_command
from test_session_time_coordination import submit
from test_timed_spell_cast import cast_command

from trpg_state_machine import (
    AdvanceSessionTime,
    DismissWorldSpell,
    StateMachine,
    world_command_from_data,
)

pytest_plugins = ("test_timed_spell_cast",)


def _hp(sm, sid, package, context, target):
    snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
    actor = next(a for a in snapshot.character_states if a.entity_id == target)
    return actor.hp_current, actor.hp_max, snapshot.effect_states


def test_aid_stronger_then_weaker_fallback_and_natural_expiry(timed_spell_world, tmp_path):
    session = timed_spell_world(
        spell_id="aid",
        caster_class="cleric",
        caster_subclass="life",
        configure=lambda data: data["rule_bindings"].pop("combat_foundation"),
    )
    sm, sid, package, context = session
    loader = sm._world.loader
    with sm:
        first = cast_command(
            session,
            spell_id="aid",
            source_id="class:cleric",
            slot_level=3,
            command_id="aid.third",
        )
        envelope = asdict(first)
        envelope["payload"] = first.payload.model_dump(mode="json")
        envelope.pop("check_world_effects")
        envelope.pop("time_wait")
        first = world_command_from_data(envelope)
        target = first.actor_id
        current, maximum, _ = _hp(sm, sid, package, context, target)
        receipt = sm.submit_command(first, package, context=context)
        assert receipt.status == "committed", receipt
        assert _hp(sm, sid, package, context, target)[:2] == (current + 10, maximum + 10)
        assert submit(session, AdvanceSessionTime(10), "aid.tick").status == "committed"
        weaker = cast_command(
            session,
            spell_id="aid",
            source_id="class:cleric",
            slot_level=2,
            command_id="aid.second",
        )
        receipt = sm.submit_command(weaker, package, context=context)
        assert receipt.status == "committed", receipt
        assert _hp(sm, sid, package, context, target)[:2] == (current + 10, maximum + 10)
        assert submit(session, AdvanceSessionTime(28800), "aid.first.end").status == "committed"
        assert _hp(sm, sid, package, context, target)[:2] == (current + 5, maximum + 5)
        assert submit(session, AdvanceSessionTime(28810), "aid.second.end").status == "committed"
        assert _hp(sm, sid, package, context, target)[:2] == (current, maximum)
    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        assert _hp(reopened, sid, package, context, target) == (current, maximum, ())


def test_aid_pair_write_failure_rolls_back_payment_and_effect(timed_spell_world, monkeypatch):
    session = timed_spell_world(
        spell_id="aid",
        caster_class="cleric",
        caster_subclass="life",
        configure=lambda data: data["rule_bindings"].pop("combat_foundation"),
    )
    sm, sid, package, context = session
    with sm:
        cast = cast_command(
            session,
            spell_id="aid",
            source_id="class:cleric",
            slot_level=2,
            command_id="aid.rollback",
        )
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        slots = before.resource_state

        def fail(*args):
            raise RuntimeError("injected pair write failure")

        with monkeypatch.context() as patch:
            patch.setattr(sm._characters, "set_hp_pair", fail)
            with pytest.raises(RuntimeError, match="injected pair write failure"):
                sm.submit_command(cast, package, context=context)
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_noncombat_snapshot(sid, package, context=context).resource_state == slots
        assert sm.submit_command(cast, package, context=context).status == "committed"


def test_aid_exact_original_caster_dismissal_restores_base(timed_spell_world):
    session = timed_spell_world(
        spell_id="aid",
        caster_class="cleric",
        caster_subclass="life",
        configure=lambda data: data["rule_bindings"].pop("combat_foundation"),
    )
    sm, sid, package, context = session
    with sm:
        cast = cast_command(
            session,
            spell_id="aid",
            source_id="class:cleric",
            slot_level=2,
            command_id="aid.dismissable",
        )
        target = cast.actor_id
        base = _hp(sm, sid, package, context, target)[:2]
        assert sm.submit_command(cast, package, context=context).status == "committed"
        (effect,) = _hp(sm, sid, package, context, target)[2]
        dismiss = world_command(
            sm,
            sid,
            package,
            context,
            DismissWorldSpell(target, effect.id, effect.origin, cast.command_id),
            "aid.dismiss",
        )
        receipt = sm.submit_command(dismiss, package, context=context)
        assert receipt.status == "committed", receipt
        assert _hp(sm, sid, package, context, target) == (*base, ())


def test_cure_wounds_heals_during_aid_without_changing_base_maximum(timed_spell_world):
    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["pc_templates"][0]["character"]["hp"]["current"] = 4
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][0][
            "hp_current"
        ] = 4

    session = timed_spell_world(
        spell_id="aid",
        caster_class="cleric",
        caster_subclass="life",
        additional_spells=("cure-wounds",),
        configure=configure,
    )
    sm, sid, package, context = session
    with sm:
        aid = cast_command(
            session,
            spell_id="aid",
            source_id="class:cleric",
            slot_level=2,
            command_id="aid.before.healing",
        )
        target = aid.actor_id
        before_current, before_max, _ = _hp(sm, sid, package, context, target)
        assert sm.submit_command(aid, package, context=context).status == "committed"
        heal = cast_command(
            session,
            spell_id="cure-wounds",
            source_id="class:cleric",
            slot_level=1,
            command_id="cure.during.aid",
        )
        receipt = sm.submit_command(heal, package, context=context)
        assert receipt.status == "committed", receipt
        healed, boosted_max, _ = _hp(sm, sid, package, context, target)
        assert before_current + 5 < healed <= boosted_max == before_max + 5
        assert submit(session, AdvanceSessionTime(28800), "aid.after.healing").status == "committed"
        assert _hp(sm, sid, package, context, target)[:2] == (
            min(before_max, healed - 5),
            before_max,
        )


@pytest.mark.parametrize("end", ["expiry", "dismissal"])
def test_aid_loss_to_zero_has_non_damage_life_consequence(timed_spell_world, tmp_path, end):
    session = timed_spell_world(
        spell_id="aid",
        caster_class="cleric",
        caster_subclass="life",
        configure=lambda data: data["rule_bindings"].pop("combat_foundation"),
    )
    sm, sid, package, context = session
    loader = sm._world.loader
    with sm:
        cast = cast_command(
            session,
            spell_id="aid",
            source_id="class:cleric",
            slot_level=2,
            command_id=f"aid.zero.{end}",
        )
        target = cast.actor_id
        original_max = _hp(sm, sid, package, context, target)[1]
        assert sm.submit_command(cast, package, context=context).status == "committed"
        (effect,) = _hp(sm, sid, package, context, target)[2]
        # This explicitly supplied intervening HP outcome is not a claim that
        # an exploration damage command exists in the production dispatcher.
        with sm._transaction(write=True):
            sm._characters.set_hp(sid, target, 3)
        if end == "expiry":
            receipt = submit(session, AdvanceSessionTime(28800), "aid.zero.expire")
        else:
            dismiss = world_command(
                sm,
                sid,
                package,
                context,
                DismissWorldSpell(target, effect.id, effect.origin, cast.command_id),
                "aid.zero.dismiss",
            )
            receipt = sm.submit_command(dismiss, package, context=context)
        assert receipt.status == "committed", receipt
        actor = next(
            actor
            for actor in sm.read_noncombat_snapshot(sid, package, context=context).character_states
            if actor.entity_id == target
        )
        assert (actor.hp_current, actor.hp_max, actor.is_alive, actor.death_saves) == (
            0,
            original_max,
            True,
            None,
        )
        assert [c.condition for c in actor.conditions] == ["unconscious"]
        assert _hp(sm, sid, package, context, target)[2] == ()
    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        actor = next(
            actor
            for actor in reopened.read_noncombat_snapshot(
                sid, package, context=context
            ).character_states
            if actor.entity_id == target
        )
        assert actor.hp_current == 0
        assert [c.condition for c in actor.conditions] == ["unconscious"]


def test_aid_zero_dismissal_failure_rolls_back_and_retries(timed_spell_world, monkeypatch):
    session = timed_spell_world(
        spell_id="aid",
        caster_class="cleric",
        caster_subclass="life",
        configure=lambda data: data["rule_bindings"].pop("combat_foundation"),
    )
    sm, sid, package, context = session
    with sm:
        cast = cast_command(
            session,
            spell_id="aid",
            source_id="class:cleric",
            slot_level=2,
            command_id="aid.zero.rollback.cast",
        )
        target = cast.actor_id
        assert sm.submit_command(cast, package, context=context).status == "committed"
        (effect,) = _hp(sm, sid, package, context, target)[2]
        with sm._transaction(write=True):
            sm._characters.set_hp(sid, target, 3)
        dismiss = world_command(
            sm,
            sid,
            package,
            context,
            DismissWorldSpell(target, effect.id, effect.origin, cast.command_id),
            "aid.zero.rollback.dismiss",
        )
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        with monkeypatch.context() as failure:
            failure.setattr(
                sm._characters,
                "add_condition",
                lambda *_args: (_ for _ in ()).throw(RuntimeError("condition write failed")),
            )
            with pytest.raises(RuntimeError, match="condition write failed"):
                sm.submit_command(dismiss, package, context=context)
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        receipt = sm.submit_command(dismiss, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(dismiss, package, context=context) == receipt
        actor = next(
            actor
            for actor in sm.read_noncombat_snapshot(sid, package, context=context).character_states
            if actor.entity_id == target
        )
        assert actor.hp_current == 0
        assert [condition.condition for condition in actor.conditions] == ["unconscious"]
