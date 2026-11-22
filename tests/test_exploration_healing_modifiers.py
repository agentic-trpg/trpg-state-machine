"""Exercise public adoption of canonical exploration healing modifiers."""

from random import Random

import pytest
from dnd5e_engine import (
    evaluation_exploration_healing,
    evaluation_speed_effects,
    evaluation_world_spell_modifiers,
)
from dnd5e_engine.evaluation_rng import RNGState, RNGTransition
from item_support import NPC
from test_cure_wounds import actor
from test_pending_spell_choice import reply
from test_session_time_coordination import submit
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import AdvanceSessionTime, StateMachine, StateMachineError

BEACON = "beacon-of-hope"
CURE_WOUNDS = "cure-wounds"
CHANNEL_DIVINITY = "channel-divinity-cleric"
CHANNEL_DIVINITY_POOL = f"feature:{CHANNEL_DIVINITY}"
DATABASE = "exploration-healing-modifiers.sqlite"
SEED = 1


def life_cleric_world(timed_spell_world, *, target_hp=2, additional_spells=()):
    """Bind a fifth-level Life cleric and author distinct injured recipients."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["pc_templates"][0]["character"]["hp"].update(current=2, max=60)
        snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
        caster, target = snapshot["character_states"]
        caster.update(
            wisdom=16,
            hp_current=2,
            hp_max=60,
            custom_counters={f"feature_use:{CHANNEL_DIVINITY}": {"spent": 0}},
        )
        target.update(hp_current=target_hp, hp_max=60, ac=14)
        spells = snapshot["casting_state"]["actors"][0]["sources"][0]["spells"]
        spells[:] = [spell for spell in spells if spell["spell_id"] in (BEACON, CURE_WOUNDS)]
        (pool,) = (
            pool
            for pool in snapshot["resource_state"]["pools"]
            if pool["pool_id"] == CHANNEL_DIVINITY_POOL
        )
        assert (pool["owner_id"], pool["feature_slug"], pool["current"], pool["maximum"]) == (
            caster["entity_id"],
            CHANNEL_DIVINITY,
            2,
            2,
        )

    return timed_spell_world(
        database=DATABASE,
        seed=SEED,
        material=False,
        caster_class="cleric",
        caster_subclass="life",
        additional_spells=(BEACON, CURE_WOUNDS, *additional_spells),
        configure=configure,
    )


def balance(snapshot, pool_id):
    """Read the balance of one authored spell or feature resource."""
    return next(pool.current for pool in snapshot.resource_state.pools if pool.pool_id == pool_id)


def committed_events(sm, sid, package, context, receipt):
    """Return only the engine evidence committed with the specified receipt."""
    return [
        event.payload["engine_event"]
        for event in sm.read_events(sid, package, context=context)
        if event.event_seq in receipt.event_seqs and "engine_event" in event.payload
    ]


@pytest.mark.parametrize("target_hp", [2, 45], ids=["uncapped", "capped"])
def test_beacon_persists_and_maximizes_an_upcast_life_cleric_heal(
    timed_spell_world, tmp_path, monkeypatch, target_hp
):
    """A real Beacon cast persists through reopen and changes only its recipient's healing."""
    life_cleric = life_cleric_world(timed_spell_world, target_hp=target_hp)
    sm, sid, package, context = life_cleric
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        beacon = cast_command(
            life_cleric, spell_id=BEACON, source_id="class:cleric", slot_level=3, target_id=NPC
        )
        caster = actor(before, beacon.actor_id)
        assert caster.classes == {"cleric": 5}
        assert caster.subclass_slug == "life" and "disciple-of-life" in caster.granted_features

        receipt = sm.submit_command(beacon, package, context=context)
        assert receipt.status == "committed", receipt
        active = sm.read_noncombat_snapshot(sid, package, context=context)
        (effect,) = active.effect_states
        assert (effect.id, effect.target_id, effect.duration.seconds) == ("effect:hopeful", NPC, 60)
        assert effect.flags["concentration"]
        assert {change.key for change in effect.changes} == {
            "flags.advantage.save.wisdom",
            "flags.advantage.save.death",
            "healing.received.maximize",
        }
        assert balance(active, "spell:3") == balance(before, "spell:3") - 1
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.submit_command(beacon, package, context=context) == receipt
        assert sm.read_noncombat_snapshot(sid, package, context=context) == active
        loader = sm._characters.loader

    with StateMachine(tmp_path / DATABASE, dnd_loader=loader) as reopened:
        session = reopened, sid, package, context

        def read():
            return reopened.read_noncombat_snapshot(sid, package, context=context)

        def heal(key, *, target_id=None, slot_level=1):
            return cast_command(
                session,
                spell_id=CURE_WOUNDS,
                source_id="class:cleric",
                command_id=key,
                target_id=target_id,
                slot_level=slot_level,
            )

        assert read() == active
        ordinary = heal("life-cleric-self")
        ordinary_receipt = reopened.submit_command(ordinary, package, context=context)
        assert ordinary_receipt.status == "committed", ordinary_receipt
        control = Random(SEED)
        rolled = control.randint(1, 8) + control.randint(1, 8)
        assert actor(read(), ordinary.actor_id).hp_current == caster.hp_current + rolled + 3 + 3
        after_ordinary = read()
        rng_after_ordinary = reopened.read_rng_context(sid, package, context=context)
        assert rng_after_ordinary.state == RNGState.capture(control)

        maximized = heal("life-cleric-upcast", target_id=NPC, slot_level=2)
        events_before = reopened.read_events(sid, package, context=context)

        def assert_unpublished():
            assert (
                reopened.get_command_receipt(sid, maximized.command_id, package, context=context)
                is None
            )
            assert read() == after_ordinary
            assert reopened.read_events(sid, package, context=context) == events_before
            assert reopened.read_rng_context(sid, package, context=context) == rng_after_ordinary

        evaluate = reopened._rules._evaluate_dnd

        async def forged_rng(request):
            result = await evaluate(request)
            assert result.status == "accepted" and not result.rng_transition.state_changed
            extra = request.rng_context.state.restore()
            extra.randint(1, 8)
            return result.model_copy(
                update={"rng_transition": RNGTransition.between(request.rng_context, extra)}
            )

        with monkeypatch.context() as guard:
            guard.setattr(reopened._rules, "_evaluate_dnd", forged_rng)
            with pytest.raises(StateMachineError) as invalid:
                reopened.submit_command(maximized, package, context=context)
            assert invalid.value.code == "invalid_rule_result"
        assert_unpublished()

        def fail_receipt(*_args):
            raise StateMachineError("storage_error", "injected healing receipt failure")

        with monkeypatch.context() as failure:
            failure.setattr(reopened._session_runtime, "record_receipt", fail_receipt)
            with pytest.raises(StateMachineError, match="injected healing receipt failure"):
                reopened.submit_command(maximized, package, context=context)
        assert_unpublished()

        paid = reopened.submit_command(maximized, package, context=context)
        assert paid.status == "committed", paid
        healed = read()
        amount = 4 * 8 + 3 + (2 + 2)
        recipient = actor(after_ordinary, NPC)
        assert actor(healed, NPC).hp_current == min(recipient.hp_max, recipient.hp_current + amount)
        assert balance(healed, "spell:2") == balance(after_ordinary, "spell:2") - 1
        assert balance(healed, CHANNEL_DIVINITY_POOL) == balance(before, CHANNEL_DIVINITY_POOL)
        assert healed.effect_states == (effect,)
        evidence = committed_events(reopened, sid, package, context, paid)
        assert [event["type"] for event in evidence] == [
            "intent_submitted",
            "spell_cast",
            "healing_applied",
        ]
        assert evidence[1]["slot_level"] == 2
        assert (evidence[2]["target_id"], evidence[2]["amount"]) == (NPC, amount)
        assert reopened.submit_command(maximized, package, context=context) == paid
        assert read() == healed
        assert reopened.read_rng_context(sid, package, context=context) == rng_after_ordinary

        assert submit(session, AdvanceSessionTime(59), "beacon-last-tick").status == "committed"
        assert read().effect_states == (effect,)
        assert submit(session, AdvanceSessionTime(60), "expire-beacon").status == "committed"
        assert not read().effect_states
        expired = reopened.submit_command(
            heal("heal-after-expiry", target_id=NPC), package, context=context
        )
        assert expired.status == "committed", expired
        amount = control.randint(1, 8) + control.randint(1, 8) + 3 + 3
        restored = actor(healed, NPC)
        assert actor(read(), NPC).hp_current == min(restored.hp_max, restored.hp_current + amount)
        after_expiry = committed_events(reopened, sid, package, context, expired)[-1]
        assert (after_expiry["type"], after_expiry["amount"]) == ("healing_applied", amount)
        assert reopened.read_rng_context(sid, package, context=context).state == RNGState.capture(
            control
        )


def test_reviewed_barkskin_is_neutral_to_public_healing(timed_spell_world, monkeypatch):
    """Exercise Barkskin consent and carry the committed effect into the healing fixture."""

    def configure_druid(data):
        data["rule_bindings"].pop("combat_foundation")
        snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
        snapshot["character_states"][1]["ac"] = 14

    druid = timed_spell_world(
        caster_class="druid",
        caster_subclass="land",
        additional_spells=("barkskin",),
        database="barkskin-source.sqlite",
        configure=configure_druid,
    )
    sm, sid, package, context = druid
    with sm:
        cast = cast_command(
            druid, spell_id="barkskin", source_id="class:druid", slot_level=2, target_id=NPC
        )
        pending = sm.submit_command(cast, package, context=context)
        assert pending.status == "pending_choice", pending
        accepted = sm.submit_command(reply(druid, pending), package, context=context)
        assert accepted.status == "committed", accepted
        (armor,) = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        assert (armor.target_id, armor.duration.seconds) == (NPC, 3600)
        assert sm.read_character_ac(sid, NPC, package, context=context).effective == 17

    # Each Session admits exploration casts from its selected PC. Carry the
    # canonical effect through the effect owner for the other caster's fixture.
    cleric = life_cleric_world(timed_spell_world, additional_spells=("barkskin",))
    sm, sid, package, context = cleric
    with sm:
        with sm._transaction(write=True):
            sm._characters.effects.add_effect(
                sid, armor.model_dump(mode="json"), expires_tick=armor.duration.seconds
            )
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        initial_ac = sm.read_character_ac(sid, NPC, package, context=context)
        assert before.effect_states == (armor,)
        assert (initial_ac.base, initial_ac.effective) == (14, 17)
        heal = cast_command(cleric, spell_id=CURE_WOUNDS, source_id="class:cleric", target_id=NPC)

        def unexpected_walking_projection(*_args):
            pytest.fail("healing evaluated a walking-speed modifier")

        with monkeypatch.context() as guard:
            for module in (evaluation_speed_effects, evaluation_world_spell_modifiers):
                guard.setattr(module, "world_walk_bonus", unexpected_walking_projection)
            guard.setattr(
                evaluation_exploration_healing,
                "world_walk_bonus",
                unexpected_walking_projection,
                raising=False,
            )
            receipt = sm.submit_command(heal, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert after.effect_states == (armor,)
        assert sm.read_character_ac(sid, NPC, package, context=context) == initial_ac
        control = Random(SEED)
        amount = control.randint(1, 8) + control.randint(1, 8) + 3 + 3
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current + amount
        assert balance(after, "spell:1") == balance(before, "spell:1") - 1
        assert sm.read_rng_context(sid, package, context=context).state == RNGState.capture(control)
