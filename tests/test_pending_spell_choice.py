"""Exercise durable consent for canonical exploration spell casting."""

import asyncio
from dataclasses import replace

import pytest
from dnd5e_engine import evaluation_contracts
from item_support import NPC, A
from pydantic import ValidationError
from spatial_support import submit as submit_world
from test_object_spatial_service import changed_grid
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    AdvanceSessionTime,
    ChoiceRespondOperationPayload,
    StateMachine,
    StateMachineError,
    StepActor,
    TypedCommand,
)
from trpg_state_machine.perception import GameViewContext

DATABASE = "pending-spell-choice.sqlite"
MARKERS = {
    24: "object.darkvision_near",
    30: "object.darkvision_edge",
    31: "object.darkvision_beyond",
    32: "object.darkvision_lit",
}
VISIBLE_IN_DARK = {MARKERS[32]}
VISIBLE_WITH_DARKVISION = {MARKERS[24], MARKERS[30], MARKERS[32]}


@pytest.fixture
def darkvision_world(timed_spell_world):
    """Bind the actual 150-foot Darkvision spell and its touch-range target."""

    def configure(data):
        changed_grid(data, width=36, default_lighting="dark", lighting={"32,0": "bright"})
        data["manifest"]["schema_version"] = "state-machine-foundation/6"
        data["lockable_doors"] = []
        next(actor for actor in data["actors"] if actor["actor_id"] == NPC)["initial_cell"] = "0,1"
        for column, object_id in MARKERS.items():
            data["scenes"][0]["objects"].append(
                {
                    "object_id": object_id,
                    "state": None,
                    "movable": False,
                    "blocks_passage": False,
                    "initial_cell": f"{column},0",
                }
            )
            data.setdefault("world_objects", []).append(
                {
                    "object_id": object_id,
                    "definition_ref": "definition." + object_id.split(".", 1)[1],
                    "display_name": object_id,
                    "description_ref": None,
                    "material": None,
                    "size_category": "small",
                    "weight_lb": 1,
                    "capabilities": [],
                }
            )

    return timed_spell_world(
        additional_spells=("darkvision",), configure=configure, database=DATABASE
    )


def darkvision_cast(session, target_id=None):
    """Request the canonical spell at its actual second-level slot."""
    command = cast_command(session, spell_id="darkvision", target_id=target_id)
    return replace(command, payload=command.payload.model_copy(update={"slot_level": 2}))


def reply(session, pending, choice="accept", *, key="answer", actor_id=NPC):
    """Build a typed answer using the public pending receipt and current world version."""
    sm, sid, package, context = session
    return TypedCommand(
        sid,
        key,
        actor_id,
        "choice.respond",
        ChoiceRespondOperationPayload(pending.pending_choice.pending_operation_id, choice),
        sm.read_session(sid, package, context=context).world_version,
    )


def current_state(session, slot_level=2):
    """Read the authoritative slot, RNG, ongoing effects and world version."""
    sm, sid, package, context = session
    snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
    (slot,) = [
        pool for pool in snapshot.resource_state.pools if pool.pool_id == f"spell:{slot_level}"
    ]
    rng = sm.read_rng_context(sid, package, context=context)
    version = sm.read_session(sid, package, context=context).world_version
    return version, slot.current, rng, snapshot.effect_states


def views(session):
    """Read visible marker IDs for the player and the controlled NPC."""
    sm, sid, package, context = session
    viewer = GameViewContext(context.principal_id, sid, context.player_id, "player")
    player = sm.get_scene_view(sid, package, context=viewer)
    npc = sm.get_spatial_view(sid, NPC, package, context=context)
    markers = set(MARKERS.values())
    return markers.intersection(dict(player.visible_objects)), markers.intersection(
        obj[0] for obj in npc.objects
    )


def pending_cast(session):
    """Submit a real cast that needs target consent."""
    sm, _, package, context = session
    command = darkvision_cast(session, NPC)
    pending = sm.submit_command(command, package, context=context)
    assert pending.status == "pending_choice", pending
    return command, pending


def test_darkvision_waits_for_target_then_adopts_once_across_restart(darkvision_world, tmp_path):
    sm, sid, package, context = darkvision_world
    with sm:
        old = current_state(darkvision_world)
        old_events = sm.read_events(sid, package, context=context)
        assert views(darkvision_world) == (VISIBLE_IN_DARK, VISIBLE_IN_DARK)
        command, pending = pending_cast(darkvision_world)
        choice = pending.pending_choice
        assert (
            choice.pending_operation_id,
            choice.authorized_responder,
            choice.choice_kind,
            choice.allowed_options,
            choice.expires_at_tick,
        ) == (command.command_id, NPC, "spell.target_willingness", ("accept", "decline"), 60)
        assert pending.world_version == old[0]
        assert pending.event_seqs == ()
        assert pending.error_code is None
        assert current_state(darkvision_world) == old
        assert sm.read_events(sid, package, context=context) == old_events
        assert views(darkvision_world) == (VISIBLE_IN_DARK, VISIBLE_IN_DARK)
        assert sm.submit_command(command, package, context=context) == pending
        loader = sm._characters.loader

    with StateMachine(tmp_path / DATABASE, dnd_loader=loader) as reopened:
        session = reopened, sid, package, context
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == pending
        )
        assert current_state(session) == old
        assert (
            submit_world(
                reopened, sid, package, context, AdvanceSessionTime(59), "last-choice-tick"
            ).status
            == "committed"
        )
        before_accept = current_state(session)
        answer = reply(session, pending)
        accepted = asyncio.run(reopened.submit_command_async(answer, package, context=context))
        assert accepted.status == "committed", accepted
        events = reopened.read_events(sid, package, context=context)
        cast_events = tuple(
            event for event in events if event.source_command_id == answer.command_id
        )
        assert cast_events
        assert accepted.event_seqs == tuple(event.event_seq for event in cast_events)
        version, slots, rng, effects = current_state(session)
        assert (version, slots, rng) == (accepted.world_version, old[1] - 1, old[2])
        assert version > before_accept[0]
        (effect,) = effects
        assert (effect.name, effect.target_id) == ("Darkvision", NPC)
        assert views(session) == (VISIBLE_IN_DARK, VISIBLE_WITH_DARKVISION)
        assert reopened.submit_command(answer, package, context=context) == accepted
        again = reopened.submit_command(
            reply(session, pending, key="answer-again"), package, context=context
        )
        assert (again.status, again.error_code) == ("rejected", "choice_closed"), again
        assert reopened.submit_command(command, package, context=context) == pending
        assert current_state(session) == (version, slots, rng, effects)

    with StateMachine(tmp_path / DATABASE, dnd_loader=loader) as reopened:
        session = reopened, sid, package, context
        assert reopened.submit_command(answer, package, context=context) == accepted
        assert views(session) == (VISIBLE_IN_DARK, VISIBLE_WITH_DARKVISION)
        assert current_state(session) == (version, slots, rng, effects)


def test_choice_decline_requires_authorized_target(darkvision_world):
    sm, sid, package, context = darkvision_world
    with sm:
        old = current_state(darkvision_world)
        command, pending = pending_cast(darkvision_world)
        valid = reply(darkvision_world, pending, "decline")
        for untrusted_command, untrusted_context in (
            (valid, replace(context, controlled_npc_ids=())),
            (replace(valid, actor_id=command.actor_id), context),
        ):
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(untrusted_command, package, context=untrusted_context)
            assert error.value.code == "unauthorized"
            assert sm.get_command_receipt(sid, valid.command_id, package, context=context) is None
        declined = sm.submit_command(valid, package, context=context)
        assert declined.status == "committed", declined
        assert declined.world_version == old[0]
        assert declined.event_seqs == ()
        assert sm.submit_command(valid, package, context=context) == declined
        again = sm.submit_command(
            reply(darkvision_world, pending, key="answer-again"), package, context=context
        )
        assert (again.status, again.error_code) == ("rejected", "choice_closed"), again
        assert current_state(darkvision_world) == old


def test_cast_rechecks_touch_range_after_consent(darkvision_world):
    sm, sid, package, context = darkvision_world
    with sm:
        _, pending = pending_cast(darkvision_world)
        original_reply = reply(darkvision_world, pending, key="stale-answer")
        for start, end in (("0,0", "1,0"), ("1,0", "2,0")):
            moved = submit_world(sm, sid, package, context, StepActor(A, start, end), f"move.{end}")
            assert moved.status == "committed", moved
        old = current_state(darkvision_world)
        stale = sm.submit_command(original_reply, package, context=context)
        assert (stale.status, stale.error_code) == ("conflict", "version_conflict"), stale
        invalid = sm.submit_command(reply(darkvision_world, pending), package, context=context)
        assert invalid.status == "rejected", invalid
        assert invalid.error_code == "target_invalid"
        assert invalid.event_seqs == ()
        assert current_state(darkvision_world) == old


def test_choice_expires_at_game_tick_deadline(darkvision_world):
    sm, sid, package, context = darkvision_world
    with sm:
        _, pending = pending_cast(darkvision_world)
        timed = submit_world(sm, sid, package, context, AdvanceSessionTime(60), "choice-deadline")
        assert timed.status == "committed", timed
        before = current_state(darkvision_world)
        answer = reply(darkvision_world, pending)
        expired = sm.submit_command(answer, package, context=context)
        assert (expired.status, expired.error_code) == ("rejected", "choice_expired"), expired
        assert expired.event_seqs == ()
        assert sm.submit_command(answer, package, context=context) == expired
        assert current_state(darkvision_world) == before


def test_failed_answer_receipt_rolls_back_slot_effect_and_pending_choice(darkvision_world):
    sm, sid, package, context = darkvision_world
    with sm:
        _, pending = pending_cast(darkvision_world)
        answer = reply(darkvision_world, pending)
        old = current_state(darkvision_world)
        old_events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_choice_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='answer' AND EXISTS ("
            "SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id "
            "AND expires_tick=28800) AND EXISTS (SELECT 1 FROM character_resources "
            "WHERE session_id=NEW.session_id AND resource_id='spell:2' AND current=2) "
            "BEGIN SELECT RAISE(ABORT,'injected choice receipt failure'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(answer, package, context=context)
        assert error.value.code == "storage_error"
        assert str(error.value.__cause__) == "injected choice receipt failure"
        assert sm.get_command_receipt(sid, answer.command_id, package, context=context) is None
        assert sm.read_events(sid, package, context=context) == old_events
        assert current_state(darkvision_world) == old
        assert views(darkvision_world) == (VISIBLE_IN_DARK, VISIBLE_IN_DARK)

        sm._conn.execute("DROP TRIGGER fail_choice_receipt")
        result = sm.submit_command(answer, package, context=context)
        assert result.status == "committed", result
        _, slots, _, effects = current_state(darkvision_world)
        assert slots == old[1] - 1
        assert len(effects) == 1


def test_self_target_darkvision_does_not_require_reply(darkvision_world):
    sm, _, package, context = darkvision_world
    with sm:
        _, slots_before, rng_before, _ = current_state(darkvision_world)
        command = darkvision_cast(darkvision_world)
        cast = sm.submit_command(command, package, context=context)
        assert cast.status == "committed", cast
        assert cast.pending_choice is None
        _, slots, rng, effects = current_state(darkvision_world)
        assert (slots, rng) == (slots_before - 1, rng_before)
        assert tuple(effect.target_id for effect in effects) == (command.actor_id,)
        assert views(darkvision_world) == (VISIBLE_WITH_DARKVISION, VISIBLE_IN_DARK)


def test_longstrider_on_another_actor_does_not_require_reply(timed_spell_world):
    session = timed_spell_world()
    sm, _, package, context = session
    with sm:
        _, slots_before, rng_before, _ = current_state(session, slot_level=1)
        cast = sm.submit_command(cast_command(session, target_id=NPC), package, context=context)
        assert cast.status == "committed", cast
        assert cast.pending_choice is None
        _, slots, rng, effects = current_state(session, slot_level=1)
        assert (slots, rng) == (slots_before - 1, rng_before)
        assert tuple(effect.target_id for effect in effects) == (NPC,)


@pytest.fixture
def barkskin_world(timed_spell_world):
    """Bind canonical Barkskin to an eligible druid and a controlled NPC."""

    def open_world(base_ac):
        def configure(data):
            bindings = data["rule_bindings"]
            bindings.pop("combat_foundation")
            snapshot = bindings["noncombat_foundation"]["snapshot"]
            target = next(
                actor for actor in snapshot["character_states"] if actor["entity_id"] == NPC
            )
            target["ac"] = base_ac

        return timed_spell_world(
            caster_class="druid",
            caster_subclass="land",
            additional_spells=("barkskin",),
            configure=configure,
        )

    return open_world


@pytest.mark.parametrize(("base_ac", "effective_ac"), [(14, 17), (19, 19)])
def test_barkskin_after_consent_applies_an_ac_floor_until_expiry(
    barkskin_world, base_ac, effective_ac
):
    session = barkskin_world(base_ac)
    sm, sid, package, context = session
    with sm:
        start = submit_world(sm, sid, package, context, AdvanceSessionTime(11), "before-cast")
        assert start.status == "committed", start
        before = current_state(session)
        initial_ac = sm.read_character_ac(sid, NPC, package, context=context)
        assert (initial_ac.base, initial_ac.effects, initial_ac.effective) == (base_ac, 0, base_ac)
        command = cast_command(
            session, spell_id="barkskin", source_id="class:druid", slot_level=2, target_id=NPC
        )
        pending = sm.submit_command(command, package, context=context)
        assert pending.status == "pending_choice", pending
        assert pending.pending_choice.authorized_responder == NPC
        assert current_state(session) == before
        assert sm.read_character_ac(sid, NPC, package, context=context) == initial_ac
        caster_ac = sm.read_character_ac(sid, command.actor_id, package, context=context)

        waiting = submit_world(sm, sid, package, context, AdvanceSessionTime(31), "still-pending")
        assert waiting.status == "committed", waiting
        assert current_state(session)[1:] == before[1:]
        answer = reply(session, pending)
        accepted = sm.submit_command(answer, package, context=context)
        assert accepted.status == "committed", accepted
        version, slots, rng, effects = current_state(session)
        assert (slots, rng) == (before[1] - 1, before[2])
        (effect,) = effects
        assert effect.target_id == NPC
        assert effect.duration.seconds == 3600
        assert effect.lifecycle is None and not effect.flags.get("concentration")
        active = sm.read_character_ac(sid, NPC, package, context=context)
        assert (active.base, active.effects, active.effective) == (
            base_ac,
            effective_ac - base_ac,
            effective_ac,
        )
        assert sm.read_character_ac(sid, command.actor_id, package, context=context) == caster_ac
        assert sm.submit_command(answer, package, context=context) == accepted
        assert current_state(session) == (version, slots, rng, effects)

        for tick, expected in ((3630, active), (3631, initial_ac)):
            advance = submit_world(
                sm, sid, package, context, AdvanceSessionTime(tick), f"tick-{tick}"
            )
            assert advance.status == "committed", advance
            assert sm.read_character_ac(sid, NPC, package, context=context) == expected
        assert current_state(session)[1:] == (slots, rng, ())


def test_declined_barkskin_has_no_ac_effect_or_slot_cost(barkskin_world):
    session = barkskin_world(14)
    sm, sid, package, context = session
    with sm:
        initial = current_state(session)
        initial_ac = sm.read_character_ac(sid, NPC, package, context=context)
        command = cast_command(
            session, spell_id="barkskin", source_id="class:druid", slot_level=2, target_id=NPC
        )
        pending = sm.submit_command(command, package, context=context)
        assert pending.status == "pending_choice", pending
        declined = sm.submit_command(reply(session, pending, "decline"), package, context=context)
        assert declined.status == "committed", declined
        assert declined.event_seqs == ()
        assert current_state(session) == initial
        assert sm.read_character_ac(sid, NPC, package, context=context) == initial_ac


def test_public_spell_payload_cannot_claim_willingness():
    with pytest.raises(ValidationError) as error:
        evaluation_contracts.ExplorationSpellCastPayload.model_validate(
            {
                "kind": "world.cast_spell",
                "spell_id": "darkvision",
                "spell_source_id": "class:wizard",
                "target_id": NPC,
                "slot_level": 2,
                "willing_target_id": NPC,
            }
        )
    (failure,) = error.value.errors()
    assert (failure["loc"], failure["type"]) == (("willing_target_id",), "extra_forbidden")
