"""Public Guidance cast and repeated matching skill checks."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_srd_data import BundledAssetLoader
from item_support import NPC
from spatial_support import submit as submit_world
from test_real_checks import check_command
from test_session_time_coordination import submit as submit_time
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    AdvanceSessionTime,
    ChoiceRespondOperationPayload,
    DropWorldConcentration,
    StateMachine,
    StateMachineError,
    TypedCommand,
    world_command_from_data,
)


def test_guidance_cast_and_repeated_checks(timed_spell_world):
    session = timed_spell_world(
        spell_id="guidance",
        caster_class="cleric",
        caster_subclass="life",
        material=False,
        configure=lambda data: data["rule_bindings"].pop("combat_foundation"),
        configure_spell=lambda _: BundledAssetLoader().get_spell("guidance"),
    )
    sm, sid, package, context = session
    context = replace(context, can_adjudicate_checks=True)
    with sm:
        balances = [
            (pool.pool_id, pool.current)
            for pool in sm.read_noncombat_snapshot(
                sid, package, context=context
            ).resource_state.pools
        ]
        cast = cast_command(
            session, spell_id="guidance", source_id="class:cleric", slot_level=0, target_id=NPC
        )
        invalid = replace(cast, command_id="guidance.missing-choice")
        before_rng = sm.read_rng_context(sid, package, context=context)
        assert sm.submit_command(invalid, package, context=context).status == "rejected"
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()
        cast = replace(
            cast,
            payload=ExplorationSpellCastPayload.model_validate(
                {**cast.payload.model_dump(), "selected_skill": "investigation"}
            ),
        )
        cast = world_command_from_data(
            {
                "schema_version": "sm-command/1",
                "session_id": sid,
                "command_id": cast.command_id,
                "actor_id": cast.actor_id,
                "command_type": cast.command_type,
                "expected_world_version": cast.expected_world_version,
                "payload": cast.payload.model_dump(mode="json"),
            }
        )
        receipt = sm.submit_command(cast, package, context=context)
        assert receipt.status == "pending_choice", receipt
        answer = TypedCommand(
            sid,
            "guidance.accept",
            NPC,
            "choice.respond",
            ChoiceRespondOperationPayload(receipt.pending_choice.pending_operation_id, "accept"),
            sm.read_session(sid, package, context=context).world_version,
        )
        receipt = sm.submit_command(answer, package, context=context)
        assert receipt.status == "committed", receipt
        (cast_event,) = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs
            and event.event_type == "world.spell_cast"
            and event.payload["engine_event"]["type"] == "spell_cast"
        ]
        assert cast_event["slot_level"] is None
        assert [
            (pool.pool_id, pool.current)
            for pool in sm.read_noncombat_snapshot(
                sid, package, context=context
            ).resource_state.pools
        ] == balances
        (effect,) = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        assert effect.changes[0].key == "system.skills.inv.bonuses.check"
        for index in range(2):
            before = sm.read_rng_context(sid, package, context=context)
            rng = before.state.restore()
            natural, bonus = rng.randint(1, 20), rng.randint(1, 4)
            check = check_command(
                sm,
                sid,
                package,
                context,
                f"guidance.check.{index}",
                actor_id=NPC,
                ability="int",
                skill="investigation",
            )
            result = sm.submit_command(check, package, context=context)
            assert result.status == "committed", result
            (event,) = [
                event
                for event in sm.read_events(sid, package, context=context)
                if event.event_seq in result.event_seqs and event.event_type == "rules.check_rolled"
            ]
            detail = event.payload["engine_event"]
            assert (detail["natural"], detail["roll_total"]) == (
                natural,
                natural + detail["modifier"],
            )
            source = f"effect:{effect.id}:{effect.origin}:check.skill.investigation.bonus"
            assert any(
                modifier["kind"] == "die"
                and modifier["face"] == bonus
                and modifier["source"] == source
                for modifier in detail["roll_detail"]["modifiers"]
            )
            assert sm.read_rng_context(sid, package, context=context).state == RNGState.capture(rng)
        other = check_command(
            sm,
            sid,
            package,
            context,
            "guidance.other",
            actor_id=NPC,
            ability="wis",
            skill="perception",
        )
        result = sm.submit_command(other, package, context=context)
        assert result.status == "committed", result
        (other_event,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in result.event_seqs and event.event_type == "rules.check_rolled"
        ]
        assert not any(
            modifier["kind"] == "die"
            for modifier in other_event.payload["engine_event"]["roll_detail"]["modifiers"]
        )
        assert submit_time(session, AdvanceSessionTime(60), "guidance.expire").status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()

        after = check_command(
            sm,
            sid,
            package,
            context,
            "guidance.after",
            actor_id=NPC,
            ability="int",
            skill="investigation",
        )
        result = sm.submit_command(after, package, context=context)
        assert result.status == "committed", result
        (after_event,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in result.event_seqs and event.event_type == "rules.check_rolled"
        ]
        assert not any(
            modifier["kind"] == "die"
            for modifier in after_event.payload["engine_event"]["roll_detail"]["modifiers"]
        )
        assert sm.submit_command(cast, package, context=context).status == "pending_choice"
        again = cast_command(
            session,
            spell_id="guidance",
            source_id="class:cleric",
            slot_level=0,
            target_id=NPC,
            command_id="guidance.again",
        )
        again = replace(
            again, payload=again.payload.model_copy(update={"selected_skill": "investigation"})
        )
        waiting = sm.submit_command(again, package, context=context)
        assert waiting.status == "pending_choice"
        response = TypedCommand(
            sid,
            "guidance.again.accept",
            NPC,
            "choice.respond",
            ChoiceRespondOperationPayload(waiting.pending_choice.pending_operation_id, "accept"),
            sm.read_session(sid, package, context=context).world_version,
        )
        assert sm.submit_command(response, package, context=context).status == "committed"
        assert len(sm.read_noncombat_snapshot(sid, package, context=context).effect_states) == 1
        assert (
            submit_world(
                sm, sid, package, context, DropWorldConcentration(), "guidance.drop"
            ).status
            == "committed"
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()


def test_guidance_rolls_back_then_survives_cold_reopen(timed_spell_world, tmp_path):
    session = timed_spell_world(
        spell_id="guidance",
        caster_class="cleric",
        caster_subclass="life",
        material=False,
        configure=lambda data: data["rule_bindings"].pop("combat_foundation"),
        configure_spell=lambda _: BundledAssetLoader().get_spell("guidance"),
    )
    sm, sid, package, context = session
    loader = sm._world.loader
    with sm:
        cast = cast_command(session, spell_id="guidance", source_id="class:cleric", slot_level=0)
        cast = replace(
            cast, payload=cast.payload.model_copy(update={"selected_skill": "investigation"})
        )
        prior = sm.read_noncombat_snapshot(sid, package, context=context)
        prior_rng = sm.read_rng_context(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_guidance_receipt BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(StateMachineError) as fault:
            sm.submit_command(cast, package, context=context)
        assert fault.value.code == "storage_error"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == prior
        assert sm.read_rng_context(sid, package, context=context) == prior_rng
        assert sm.get_command_receipt(sid, cast.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_guidance_receipt")
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        assert len(before) == 1
    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        assert (
            reopened.read_noncombat_snapshot(sid, package, context=context).effect_states == before
        )
        assert reopened.submit_command(cast, package, context=context).status == "committed"
        assert (
            reopened.read_noncombat_snapshot(sid, package, context=context).effect_states == before
        )
