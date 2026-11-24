"""Public Bane casts and their action-local saving throw choices."""

import asyncio
import random
from copy import deepcopy
from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_bardic_grant import ExplorationFeaturePayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_saves import BARDIC_INSPIRATION_SOURCE
from item_support import NPC
from test_timed_spell_cast import first_level_balance
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    StateMachine,
    StateMachineError,
    TrustedContext,
    TypedCommand,
    rule_execution,
    session_checkpoints,
    world_command_from_data,
)
from trpg_state_machine.choice_models import ChoiceRespondOperationPayload

OTHER = "npc.attendant"


def _world(timed_spell_world, seed):
    """Prepare a Bard caster, a nearby Bard grantor and an ordered Bane cast."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["actors"][0]["initial_cell"] = "2,0"
        data["actors"].append(
            {**deepcopy(data["actors"][0]), "actor_id": OTHER, "initial_cell": "1,1"}
        )
        data["scenes"][0]["actor_ids"].append(OTHER)
        data["navigation"]["actor_grants"].append(
            {"actor_id": OTHER, "region_ids": ["region.public"]}
        )
        foundation = data["rule_bindings"]["noncombat_foundation"]
        foundation["xp"][OTHER] = 0
        snapshot = foundation["snapshot"]
        pc, npc = snapshot["character_states"]
        snapshot["character_states"].append(
            {**deepcopy(npc), "entity_id": OTHER, "name": "Attendant", "charisma": 10}
        )
        pc["charisma"] = 16
        npc.update(
            class_slug="bard",
            classes={"bard": 5},
            subclass_slug="lore",
            character_level=5,
            granted_features=pc["granted_features"],
            charisma=16,
            custom_counters={"feature_use:bardic-inspiration": {"spent": 0}},
        )
        snapshot["resource_state"]["pools"].append(
            {
                "kind": "feature_uses",
                "owner_id": NPC,
                "pool_id": "bardic-inspiration",
                "current": 3,
                "maximum": 3,
                "feature_slug": "bardic-inspiration",
            }
        )

    return timed_spell_world(
        database="action-bane.sqlite",
        caster_class="bard",
        caster_subclass="lore",
        additional_spells=("bane",),
        seed=seed,
        configure=configure,
    )


def _grant(session):
    """Issue the grant through the public feature action."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    command = TypedCommand(
        sid,
        "bane.grant",
        NPC,
        "world.use_feature",
        ExplorationFeaturePayload(
            kind="world.use_feature", feature_id="bardic-inspiration", target_id=state.pc_actor_id
        ),
        state.world_version,
    )
    assert sm.submit_command(command, package, context=context).status == "committed"
    return state.pc_actor_id


def _cast(session, targets, key="bane.cast"):
    """Decode the same ordered JSON command consumed by the public World API."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    return world_command_from_data(
        {
            "schema_version": "sm-command/1",
            "session_id": sid,
            "command_id": key,
            "actor_id": state.pc_actor_id,
            "command_type": "world.cast_spell",
            "expected_world_version": state.world_version,
            "payload": {
                "kind": "world.cast_spell",
                "spell_id": "bane",
                "spell_source_id": "class:bard",
                "slot_level": 1,
                "target_id": targets[0],
                "target_ids": list(targets),
            },
        }
    )


def _reply(source, selected, *, actor=None):
    """Use the original operation ID and the pending command's expected version."""
    return TypedCommand(
        source.session_id,
        "bane.answer",
        actor or source.actor_id,
        "choice.respond",
        ChoiceRespondOperationPayload(source.command_id, selected),
        source.expected_world_version,
    )


def _rolls(session, command):
    """Read command receipt evidence with the selected PC's authorization."""
    sm, sid, package, context = session
    return sm.get_command_rolls(sid, command.command_id, package, context=context)["rolls"]


def _effects(session, command):
    """Read exact adopted Bane members for this cast."""
    sm, sid, _, _ = session
    rows = sm._conn.execute(
        "SELECT target_id FROM ongoing_effects WHERE session_id=? AND cast_command_id=? "
        "AND effect_id='effect:bane'",
        (sid, command.command_id),
    )
    return {row[0] for row in rows}


def _grant_still_held(session, pc):
    """Find the exact public issuance, if it is still active."""
    sm, sid, _, _ = session
    return sm._world.characters.effects.held_bardic_inspiration(sid, pc, 0)


@pytest.mark.parametrize(
    ("seed", "answer", "pc_succeeds", "prior_bane"),
    [
        (1, None, True, False),
        (7, "decline", False, False),
        (82, "accept", True, True),
        (2, "accept", False, False),
    ],
)
def test_public_bane_save_order_and_inspiration(
    timed_spell_world,
    tmp_path,
    seed,
    answer,
    pc_succeeds,
    prior_bane,
):
    session = _world(timed_spell_world, seed)
    sm, sid, package, context = session
    with sm:
        pc = _grant(session)
        grant = _grant_still_held(session, pc)
        assert (grant.grant_id, grant.die_sides) == ("bane.grant", 8)
        if prior_bane:
            previous = _cast(session, (NPC,), "bane.previous")
            assert sm.submit_command(previous, package, context=context).status == "committed"
            assert _effects(session, previous) == {NPC}
        source = _cast(session, (NPC, pc, OTHER))
        paid_before = first_level_balance(sm, source)
        before_rng = sm.read_rng_context(sid, package, context=context)
        expected = before_rng.state.restore()
        npc_face = expected.randint(1, 20)
        prior_d4 = expected.randint(1, 4) if prior_bane else None
        pc_face = expected.randint(1, 20)
        bardic_face = expected.randint(1, 8) if answer == "accept" else None
        other_face = expected.randint(1, 20)
        pending = sm.submit_command(source, package, context=context)
        if answer is not None:
            assert pending.status == "pending_choice", pending
            assert pending.pending_choice.authorized_responder == pc
            assert pending.pending_choice.allowed_options == ("accept", "decline")
            assert sm.submit_command(source, package, context=context) == pending
            assert sm._session_runtime.receipt(sid, source.command_id) is None
            assert sm.read_rng_context(sid, package, context=context) == before_rng
            assert first_level_balance(sm, source) == paid_before
            assert _effects(session, source) == set()
            original = _rolls(session, source)
            assert [roll["target_id"] for roll in original] == [NPC, pc]
            assert [roll["natural"] for roll in original] == [npc_face, pc_face]
            assert original[1]["succeeded"] is False
            assert _grant_still_held(session, pc) == grant
            if prior_bane:
                assert _effects(session, previous) == {NPC}
            reply = _reply(source, answer)
            receipt = asyncio.run(sm.submit_command_async(reply, package, context=context))
            assert receipt.status == "committed", receipt
            assert sm.submit_command(reply, package, context=context) == receipt
        else:
            assert pending.status == "committed", pending
        rolls = _rolls(session, source)
        assert [roll["target_id"] for roll in rolls] == [NPC, pc, OTHER]
        assert [roll["natural"] for roll in rolls] == [npc_face, pc_face, other_face]
        assert all(roll["ability"] == "cha" and roll["dc"] == 14 for roll in rolls)
        assert rolls[1]["succeeded"] is pc_succeeds
        bonus = [
            (item["sides"], item["face"], item["value"])
            for item in rolls[1]["roll_detail"]["modifiers"]
            if item["source"] == BARDIC_INSPIRATION_SOURCE
        ]
        assert bonus == ([(8, bardic_face, bardic_face)] if answer == "accept" else [])
        if answer is not None:
            assert rolls[0]["roll_detail"] == original[0]["roll_detail"]
            assert rolls[1]["roll_detail"]["d20"] == original[1]["roll_detail"]["d20"]
        if prior_bane:
            assert _effects(session, previous) == set()
            assert any(
                item["kind"] == "die"
                and item["face"] == prior_d4
                and item["value"] == -prior_d4
                and item["source"].startswith("effect:bane@")
                for item in rolls[0]["roll_detail"]["modifiers"]
            )
        assert _effects(session, source) == {
            roll["target_id"] for roll in rolls if not roll["succeeded"]
        }
        assert _grant_still_held(session, pc) == (None if answer == "accept" else grant)
        assert first_level_balance(sm, source) == paid_before - 1
        after_rng = sm.read_rng_context(sid, package, context=context)
        assert after_rng.version == before_rng.version + 1
        assert after_rng.state == RNGState.capture(expected)
        cast_receipt = sm.submit_command(source, package, context=context)
        assert cast_receipt.status == "committed"
        loader = sm._rules.loader
    with StateMachine(tmp_path / "action-bane.sqlite", dnd_loader=loader) as cold:
        assert cold.submit_command(source, package, context=context) == cast_receipt
        if answer is not None:
            assert cold.submit_command(reply, package, context=context) == receipt
        cold_session = cold, sid, package, context
        assert _rolls(cold_session, source) == rolls
        assert cold.read_rng_context(sid, package, context=context) == after_rng
        assert first_level_balance(cold, source) == paid_before - 1
        assert _grant_still_held(cold_session, pc) == (None if answer == "accept" else grant)


def test_bane_grant_without_holder_in_targets_keeps_ordinary_cast(timed_spell_world):
    """A grant on an unselected caster does not open a target's choice."""
    session = _world(timed_spell_world, 2)
    sm, _, package, context = session
    with sm:
        pc = _grant(session)
        grant = _grant_still_held(session, pc)
        command = _cast(session, (NPC, OTHER))
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert [roll["target_id"] for roll in _rolls(session, command)] == [NPC, OTHER]
        assert _grant_still_held(session, pc) == grant


def test_resumed_bane_preserves_ordinary_ward_refusal(timed_spell_world):
    """A repeated failed ward member triggers the existing rejection after resolution."""
    session = _world(timed_spell_world, 8)
    sm, sid, package, context = session
    with sm:
        pc = _grant(session)
        previous = _cast(session, (NPC,), "bane.previous")
        assert sm.submit_command(previous, package, context=context).status == "committed"
        assert _effects(session, previous) == {NPC}
        source = _cast(session, (NPC, pc, OTHER))
        rng = sm.read_rng_context(sid, package, context=context)
        grant = _grant_still_held(session, pc)
        assert sm.submit_command(source, package, context=context).status == "pending_choice"
        answer = _reply(source, "accept")
        rejected = sm.submit_command(answer, package, context=context)
        assert (rejected.status, rejected.error_code) == ("rejected", "spell.effect_exists")
        assert not sm._has_pending_action_choice(sid)
        assert _effects(session, previous) == {NPC}
        assert _effects(session, source) == set()
        assert _grant_still_held(session, pc) == grant
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert first_level_balance(sm, source) == 3


def test_bane_rechecks_disclosure_responder_version_and_expiry(timed_spell_world):
    """Other principals and actors cannot read or answer the original dice."""
    session = _world(timed_spell_world, 2)
    sm, sid, package, context = session
    with sm:
        pc = _grant(session)
        source = _cast(session, (NPC, pc, OTHER))
        assert sm.submit_command(source, package, context=context).status == "pending_choice"
        original = sm.read_rng_context(sid, package, context=context)
        outsider = TrustedContext("authenticated:other", context.player_id)
        with pytest.raises(StateMachineError) as denied_roll:
            sm.get_command_rolls(sid, source.command_id, package, context=outsider)
        assert denied_roll.value.code == "unauthorized"
        with pytest.raises(StateMachineError) as denied_reply:
            sm.submit_command(_reply(source, "accept", actor=NPC), package, context=context)
        assert denied_reply.value.code == "unauthorized"
        malformed = replace(
            _reply(source, "accept"),
            payload=ChoiceRespondOperationPayload(source.command_id, "forged-option"),
        )
        with pytest.raises(StateMachineError) as bad_option:
            asyncio.run(sm.submit_command_async(malformed, package, context=context))
        assert bad_option.value.code == "invalid_operation"
        same_key = replace(_reply(source, "accept"), command_id=source.command_id)
        with pytest.raises(StateMachineError) as bad_key:
            asyncio.run(sm.submit_command_async(same_key, package, context=context))
        assert bad_key.value.code == "idempotency_conflict"
        with pytest.raises(StateMachineError) as wrong_version:
            sm.submit_command(
                replace(_reply(source, "accept"), expected_world_version=0),
                package,
                context=context,
            )
        assert wrong_version.value.code == "version_conflict"
        assert sm._has_pending_action_choice(sid)
        # Normal Scenario writes are gated while the choice is open. Inject elapsed
        # grant validity at the owner to exercise the reply's stale-read protection.
        with sm._transaction(write=True):
            now = sm._world_facts.time_state(sid)[1]
            sm._conn.execute(
                "UPDATE ongoing_effects SET expires_tick=? WHERE session_id=? "
                "AND target_id=? AND effect_id='effect:inspired'",
                (now, sid, pc),
            )
        with pytest.raises(StateMachineError) as expired:
            sm.submit_command(_reply(source, "decline"), package, context=context)
        assert expired.value.code == "choice_stale"
        assert not sm._has_pending_action_choice(sid)
        assert sm.get_command_rolls(sid, source.command_id, package, context=context) is None
        assert sm.read_rng_context(sid, package, context=context) == original


def test_bane_reply_rejects_rebound_request_before_continuing(timed_spell_world):
    """Fresh owner facts must match the exact original bound request."""
    session = _world(timed_spell_world, 2)
    sm, sid, package, context = session
    with sm:
        pc = _grant(session)
        source = _cast(session, (NPC, pc, OTHER))
        assert sm.submit_command(source, package, context=context).status == "pending_choice"
        rng = sm.read_rng_context(sid, package, context=context)
        grant = _grant_still_held(session, pc)
        pending = sm._pending_action_choices[sid]
        pending.request = pending.request.model_copy(update={"command_id": "another.cast"})
        with pytest.raises(StateMachineError) as stale:
            sm.submit_command(_reply(source, "accept"), package, context=context)
        assert stale.value.code == "choice_stale"
        assert not sm._has_pending_action_choice(sid)
        assert sm._session_runtime.receipt(sid, source.command_id) is None
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert _grant_still_held(session, pc) == grant


def test_bane_late_reply_failure_rolls_back_both_receipts_and_keeps_prefix(
    timed_spell_world, monkeypatch
):
    """Fault after staged writes and retry the same reply from its private prefix."""
    from dnd5e_engine import evaluation_ward_spells

    session = _world(timed_spell_world, 2)
    sm, sid, package, context = session
    with sm:
        pc = _grant(session)
        source = _cast(session, (NPC, pc, OTHER))
        resolver = evaluation_ward_spells.resolve_ordered_spell_saves
        transactions = []

        def observe_rules(*args, **kwargs):
            """Observe the real solve and deterministic verification boundaries."""
            transactions.append(sm._conn.in_transaction)
            return resolver(*args, **kwargs)

        monkeypatch.setattr(evaluation_ward_spells, "resolve_ordered_spell_saves", observe_rules)
        assert sm.submit_command(source, package, context=context).status == "pending_choice"
        reply = _reply(source, "accept")
        frozen = _rolls(session, source)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm.read_events(sid, package, context=context)
        before_slots = first_level_balance(sm, source)
        before_grant = _grant_still_held(session, pc)
        record = sm._session_runtime.record_receipt

        def fail_reply(command, *args, **kwargs):
            """Fail after the source receipt and adopted spell state have been staged."""
            if command.command_id == reply.command_id:
                raise RuntimeError("late Bane reply receipt failure")
            return record(command, *args, **kwargs)

        monkeypatch.setattr(sm._session_runtime, "record_receipt", fail_reply)
        with pytest.raises(RuntimeError, match="late Bane reply receipt failure"):
            sm.submit_command(reply, package, context=context)
        assert sm._session_runtime.receipt(sid, source.command_id) is None
        assert sm._session_runtime.receipt(sid, reply.command_id) is None
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_events(sid, package, context=context) == before_events
        assert first_level_balance(sm, source) == before_slots
        assert _grant_still_held(session, pc) == before_grant
        assert _rolls(session, source) == frozen
        assert _effects(session, source) == set()
        monkeypatch.setattr(sm._session_runtime, "record_receipt", record)
        assert sm.submit_command(reply, package, context=context).status == "committed"
        assert first_level_balance(sm, source) == before_slots - 1
        assert _grant_still_held(session, pc) is None
        assert transactions == [False] * 5


@pytest.mark.parametrize("tamper", ["answer", "result_identity", "final_roll"])
def test_bane_rejects_a_mismatched_continuation_without_adoption(
    timed_spell_world, monkeypatch, tamper
):
    """A reply cannot adopt another choice, cast identity or altered die evidence."""
    session = _world(timed_spell_world, 2)
    sm, sid, package, context = session
    with sm:
        pc = _grant(session)
        source = _cast(session, (NPC, pc, OTHER))
        assert sm.submit_command(source, package, context=context).status == "pending_choice"
        rng = sm.read_rng_context(sid, package, context=context)
        grant = _grant_still_held(session, pc)
        native = rule_execution.evaluate_bound_bane_step

        def altered(request, **kwargs):
            """Return a mismatched answer or completed proposal after real resolution."""
            if tamper == "answer":
                kwargs["choice"] = False
            progress = native(request, **kwargs)
            if tamper == "result_identity":
                return replace(
                    progress, result=progress.result.model_copy(update={"command_id": "other"})
                )
            if tamper == "final_roll":
                steps = list(progress.steps)
                last = steps[-1]
                final = last.original.model_copy(update={"succeeded": not last.original.succeeded})
                steps[-1] = replace(last, original=final)
                return replace(progress, steps=tuple(steps))
            return progress

        monkeypatch.setattr(rule_execution, "evaluate_bound_bane_step", altered)
        reply = _reply(source, "accept")
        with pytest.raises(StateMachineError) as fault:
            sm.submit_command(reply, package, context=context)
        assert fault.value.code == "invalid_rule_result"
        assert sm._session_runtime.receipt(sid, source.command_id) is None
        assert sm._session_runtime.receipt(sid, reply.command_id) is None
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert _grant_still_held(session, pc) == grant
        assert sm._has_pending_action_choice(sid)
        monkeypatch.setattr(rule_execution, "evaluate_bound_bane_step", native)
        assert sm.submit_command(reply, package, context=context).status == "committed"


def test_bane_checkpoint_load_and_cold_open_discard_the_private_attempt(
    timed_spell_world, tmp_path, monkeypatch
):
    """Reload the last committed boundary and begin with an independent RNG stream."""
    session = _world(timed_spell_world, 2)
    sm, sid, package, context = session
    with sm:
        pc = _grant(session)
        checkpoint = tmp_path / "before-bane.sqlite"
        sm.create_checkpoint(sid, package, checkpoint, context=context)
        source = _cast(session, (NPC, pc, OTHER))
        original_rng = sm.read_rng_context(sid, package, context=context)
        assert sm.submit_command(source, package, context=context).status == "pending_choice"
        original = _rolls(session, source)
        assert first_level_balance(sm, source) == 4
        with pytest.raises(StateMachineError) as blocked:
            sm.create_checkpoint(sid, package, tmp_path / "mid-bane.sqlite", context=context)
        assert blocked.value.code == "pending_action_choice"
        fresh = RNGState.capture(random.Random(7))
        monkeypatch.setattr(session_checkpoints, "_new_rng_state", lambda: fresh)
        with sm.load_checkpoint(
            checkpoint, tmp_path / "loaded-bane.sqlite", sid, package, context=context
        ) as loaded:
            assert not sm._has_pending_action_choice(sid)
            assert sm.get_command_rolls(sid, source.command_id, package, context=context) is None
            assert sm.read_rng_context(sid, package, context=context) == original_rng
            loaded_session = loaded, sid, package, context
            assert (
                loaded.get_command_rolls(sid, source.command_id, package, context=context) is None
            )
            assert loaded.read_rng_context(sid, package, context=context).state == fresh
            second = _cast(loaded_session, (NPC, pc, OTHER), "bane.fresh")
            assert (
                loaded.submit_command(second, package, context=context).status == "pending_choice"
            )
            assert _rolls(loaded_session, second)[1]["natural"] != original[1]["natural"]
            loader = loaded._rules.loader
    with StateMachine(tmp_path / "loaded-bane.sqlite", dnd_loader=loader) as reopened:
        assert not reopened._has_pending_action_choice(sid)
        assert reopened.get_command_rolls(sid, second.command_id, package, context=context) is None
        assert reopened.read_rng_context(sid, package, context=context).state == fresh
        assert first_level_balance(reopened, second) == 4
        assert reopened.submit_command(second, package, context=context).status == "pending_choice"
        assert (
            reopened.get_command_rolls(sid, second.command_id, package, context=context)["rolls"][
                1
            ]["natural"]
            != original[1]["natural"]
        )
