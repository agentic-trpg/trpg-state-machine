"""Apply an authored exploration utility only to individually consenting targets."""

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import ExplorationMultiSpellCastPayload
from dnd5e_engine.evaluation_effects import effect_state
from item_support import NPC, A
from spatial_support import submit as submit_world
from test_object_spatial_service import changed_grid
from test_pending_spell_choice import current_state
from test_spell_attack_cast import OTHER_ENEMY
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    AdvanceSessionTime,
    ChoiceFinalizeSpellOperationPayload,
    ChoiceRespondOperationPayload,
    StateMachine,
    StateMachineError,
    StepActor,
    TypedCommand,
)
from trpg_state_machine.commands import world_command_from_data

SPELL = "fixture-shared-stride"
DATABASE = "multi-spell-choice.sqlite"
THIRD_NPC = "npc.third_attendant"


@pytest.fixture
def multi_world(timed_spell_world):
    """Use a frozen authored up-to-three touch utility with independent NPCs."""

    def configure_spell(spell):
        activity = spell.activities[0]
        target = activity.target
        return spell.model_copy(
            update={
                "slug": SPELL,
                "foundry_uuid": "",
                "name": "Fixture Shared Stride",
                "activities": [
                    activity.model_copy(
                        update={
                            "target": target.model_copy(
                                update={
                                    "affects": target.affects.model_copy(
                                        update={"count": "3", "type": "willing"}
                                    )
                                }
                            )
                        }
                    )
                ],
            }
        )

    def configure(data):
        changed_grid(data, width=5, default_lighting="bright", lighting={})
        data["pc_templates"][0]["initial_cell"] = "1,1"
        second = next(actor for actor in data["actors"] if actor["actor_id"] == OTHER_ENEMY)
        second["initial_cell"] = "0,1"
        third = deepcopy(second)
        third.update(actor_id=THIRD_NPC, name="Third Attendant", initial_cell="2,1")
        data["actors"].append(third)
        data["scenes"][0]["actor_ids"].append(THIRD_NPC)
        data["navigation"]["actor_grants"].append(
            {"actor_id": THIRD_NPC, "region_ids": ["region.public"]}
        )
        foundation = data["rule_bindings"]["noncombat_foundation"]
        actors = foundation["snapshot"]["character_states"]
        mechanical = deepcopy(next(actor for actor in actors if actor["entity_id"] == OTHER_ENEMY))
        mechanical.update(entity_id=THIRD_NPC, name="Third Attendant")
        actors.append(mechanical)
        foundation["xp"][THIRD_NPC] = 0

    return timed_spell_world(
        hit_rider="ray-of-frost",
        configure=configure,
        configure_spell=configure_spell,
        database=DATABASE,
    )


def _initial(session, target_ids=(NPC, OTHER_ENEMY)):
    """Submit an immutable cast selection without any willingness attestation."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    command = TypedCommand(
        sid,
        "cast.shared-stride",
        state.pc_actor_id,
        "world.cast_spell_many",
        ExplorationMultiSpellCastPayload(
            kind="world.cast_spell_many",
            spell_id=SPELL,
            spell_source_id="class:wizard",
            slot_level=1,
            target_ids=target_ids,
        ),
        state.world_version,
    )
    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "pending_choice", receipt
    return command, receipt


def _reply(session, actor_id, decision, key):
    """Build one target's own response envelope for the shared cast."""
    sm, sid, package, context = session
    return TypedCommand(
        sid,
        key,
        actor_id,
        "choice.respond",
        ChoiceRespondOperationPayload("cast.shared-stride", decision),
        sm.read_session(sid, package, context=context).world_version,
    )


def _finalize(session, target_ids, *, key="finalize"):
    """Confirm only the intended original targets through the caster's own command."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        state.pc_actor_id,
        "choice.finalize_spell",
        ChoiceFinalizeSpellOperationPayload("cast.shared-stride", target_ids),
        state.world_version,
    )


def test_independent_willing_replies_survive_restart_and_cast_once(
    multi_world, tmp_path, monkeypatch
):
    sm, sid, package, context = multi_world
    first_context = replace(context, controlled_npc_ids=(NPC,))
    second_context = replace(context, controlled_npc_ids=(OTHER_ENEMY,))
    with sm:
        before = current_state(multi_world, 1)
        original, pending = _initial(multi_world)
        assert [prompt.authorized_responder for prompt in pending.pending_choices] == [
            NPC,
            OTHER_ENEMY,
        ]
        assert (
            sm.read_multi_spell_prompt(
                sid, original.command_id, NPC, package, context=first_context
            )
            == pending.pending_choices[0]
        )
        with pytest.raises(StateMachineError, match="control grant"):
            sm.read_multi_spell_prompt(
                sid, original.command_id, OTHER_ENEMY, package, context=first_context
            )
        answer_a = _reply(multi_world, NPC, "accept", "a")
        accepted_a = sm.submit_command(answer_a, package, context=first_context)
        assert accepted_a.status == "committed"
        assert accepted_a.event_seqs == ()
        unauthorized = _reply(multi_world, OTHER_ENEMY, "accept", "not-mine")
        with pytest.raises(StateMachineError, match="control grant"):
            sm.submit_command(unauthorized, package, context=first_context)
        assert current_state(multi_world, 1) == before
        loader = sm._combat.rules.loader
    with StateMachine(tmp_path / DATABASE, dnd_loader=loader) as restored:
        session = restored, sid, package, context
        assert (
            restored.get_command_receipt(sid, original.command_id, package, context=context)
            == pending
        )
        assert restored.submit_command(answer_a, package, context=first_context) == accepted_a
        assert (
            restored.read_multi_spell_prompt(
                sid, original.command_id, OTHER_ENEMY, package, context=second_context
            )
            == pending.pending_choices[1]
        )
        answer_b = _reply(session, OTHER_ENEMY, "accept", "b")
        assert (
            restored.submit_command(answer_b, package, context=second_context).status == "committed"
        )
        assert current_state(session, 1) == before
        first = _finalize(session, (OTHER_ENEMY, NPC))
        second = _finalize(session, (NPC, OTHER_ENEMY), key="second-finalize")
        rules = restored._combat.rules
        evaluate = rules._evaluate_dnd

        async def confirm_from_two_requests():
            both_present = asyncio.Event()
            awaiting = 0

            async def wait_for_both(request):
                nonlocal awaiting
                awaiting += 1
                if awaiting == 2:
                    both_present.set()
                await asyncio.wait_for(both_present.wait(), 10)
                return await evaluate(request)

            with monkeypatch.context() as patch:
                patch.setattr(rules, "_evaluate_dnd", wait_for_both)
                return await asyncio.gather(
                    restored.submit_command_async(first, package, context=context),
                    restored.submit_command_async(second, package, context=context),
                )

        outcomes = asyncio.run(confirm_from_two_requests())
        assert {outcome.status for outcome in outcomes} == {"committed", "rejected"}, outcomes
        assert {outcome.error_code for outcome in outcomes} == {None, "choice_closed"}
        finalize, outcome = next(
            (command, receipt)
            for command, receipt in zip((first, second), outcomes, strict=True)
            if receipt.status == "committed"
        )
        assert restored.submit_command(finalize, package, context=context) == outcome
        after = current_state(session, 1)
        assert after[:3] == (before[0] + 1, before[1] - 1, before[2])
        assert {effect.target_id for effect in after[3]} == {NPC, OTHER_ENEMY}


def test_caster_can_confirm_only_a_permitted_willing_subset(multi_world):
    sm, _, package, context = multi_world
    first_context = replace(context, controlled_npc_ids=(NPC,))
    second_context = replace(context, controlled_npc_ids=(OTHER_ENEMY,))
    third_context = replace(context, controlled_npc_ids=(THIRD_NPC,))
    with sm:
        before = current_state(multi_world, 1)
        _initial(multi_world, (NPC, OTHER_ENEMY, THIRD_NPC))
        first = _reply(multi_world, NPC, "accept", "a")
        second = _reply(multi_world, OTHER_ENEMY, "decline", "b")
        third = _reply(multi_world, THIRD_NPC, "accept", "c")
        assert sm.submit_command(first, package, context=first_context).status == "committed"
        assert sm.submit_command(second, package, context=second_context).status == "committed"
        assert sm.submit_command(third, package, context=third_context).status == "committed"
        invalid = _finalize(multi_world, (NPC, THIRD_NPC, OTHER_ENEMY), key="unwilling")
        outcome = sm.submit_command(invalid, package, context=context)
        assert (outcome.status, outcome.error_code) == ("rejected", "choice_target_not_willing")
        invalid = _finalize(multi_world, (NPC, first_context.player_id), key="unselected")
        outcome = sm.submit_command(invalid, package, context=context)
        assert (outcome.status, outcome.error_code) == ("rejected", "choice_target_not_selected")
        assert current_state(multi_world, 1) == before
        outcome = sm.submit_command(
            _finalize(multi_world, (NPC, THIRD_NPC)), package, context=context
        )
        assert outcome.status == "committed", outcome
        after = current_state(multi_world, 1)
        assert after[:3] == (before[0] + 1, before[1] - 1, before[2])
        assert {effect.target_id for effect in after[3]} == {NPC, THIRD_NPC}


def test_fresh_positions_and_timeout_are_checked_at_confirmation(multi_world):
    sm, sid, package, context = multi_world
    npc_context = replace(context, controlled_npc_ids=(NPC,))
    with sm:
        _initial(multi_world)
        reply = _reply(multi_world, NPC, "accept", "a")
        assert sm.submit_command(reply, package, context=npc_context).status == "committed"
        relocated = submit_world(sm, sid, package, context, StepActor(A, "1,1", "1,2"), "move")
        assert relocated.status == "committed", relocated
        before = current_state(multi_world, 1)
        outcome = sm.submit_command(_finalize(multi_world, (NPC,)), package, context=context)
        assert (outcome.status, outcome.error_code) == ("rejected", "target_invalid")
        assert current_state(multi_world, 1) == before
        advanced = submit_world(sm, sid, package, context, AdvanceSessionTime(60), "advance")
        assert advanced.status == "committed", advanced
        outcome = sm.submit_command(
            _finalize(multi_world, (NPC,), key="late"), package, context=context
        )
        assert (outcome.status, outcome.error_code) == ("rejected", "choice_expired")
        assert current_state(multi_world, 1)[1:] == before[1:]


def test_final_receipt_failure_rolls_back_slot_effects_and_window(multi_world, monkeypatch):
    sm, _, package, context = multi_world
    with sm:
        _initial(multi_world)
        for key, target in (("a", NPC), ("b", OTHER_ENEMY)):
            answer = _reply(multi_world, target, "accept", key)
            assert (
                sm.submit_command(
                    answer, package, context=replace(context, controlled_npc_ids=(target,))
                ).status
                == "committed"
            )
        before = current_state(multi_world, 1)
        confirmation = _finalize(multi_world, (NPC, OTHER_ENEMY))
        runtime = sm._session_runtime
        finish = runtime.finish

        def fail(*args, **kwargs):
            finish(*args, **kwargs)
            raise RuntimeError("receipt fault")

        with monkeypatch.context() as patch:
            patch.setattr(runtime, "finish", fail)
            with pytest.raises(RuntimeError, match="receipt fault"):
                sm.submit_command(confirmation, package, context=context)
        assert current_state(multi_world, 1) == before
        assert sm.submit_command(confirmation, package, context=context).status == "committed"


def test_coupled_forged_effect_and_event_are_rejected_without_any_writes(multi_world, monkeypatch):
    sm, sid, package, context = multi_world
    with sm:
        _initial(multi_world)
        answer = _reply(multi_world, NPC, "accept", "a")
        assert (
            sm.submit_command(
                answer, package, context=replace(context, controlled_npc_ids=(NPC,))
            ).status
            == "committed"
        )
        confirmation = _finalize(multi_world, (NPC,))
        before = current_state(multi_world, 1)
        events_before = sm.read_events(sid, package, context=context)
        rules = sm._combat.rules
        evaluate = rules._evaluate_dnd

        async def forged_result(request):
            result = await evaluate(request)
            payment, creation = result.state_delta.operations
            intent, cast, applied = result.proposed_events
            changed_effect = applied.effect.model_copy(
                update={"duration": applied.effect.duration.model_copy(update={"seconds": 3})}
            )
            changed_application = creation.value.model_copy(
                update={"effect": effect_state(changed_effect)}
            )
            changed_creation = creation.model_copy(update={"value": changed_application})
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": (payment, changed_creation)}
                    ),
                    "proposed_events": (
                        intent,
                        cast,
                        applied.model_copy(update={"effect": changed_effect}),
                    ),
                }
            )

        with monkeypatch.context() as patch:
            patch.setattr(rules, "_evaluate_dnd", forged_result)
            with pytest.raises(StateMachineError) as invalid:
                sm.submit_command(confirmation, package, context=context)
            assert invalid.value.code == "invalid_rule_result"
        assert current_state(multi_world, 1) == before
        assert sm.read_events(sid, package, context=context) == events_before
        assert (
            sm.get_command_receipt(sid, confirmation.command_id, package, context=context) is None
        )
        assert sm.submit_command(confirmation, package, context=context).status == "committed"


def test_finalize_replay_checks_command_identity_when_pending_cast_is_missing(multi_world):
    sm, _, package, context = multi_world
    with sm:
        confirmation = _finalize(multi_world, (NPC,))
        outcome = sm.submit_command(confirmation, package, context=context)
        assert (outcome.status, outcome.error_code) == ("rejected", "choice_not_found")
        changed = replace(
            confirmation,
            payload=replace(confirmation.payload, pending_operation_id="different-missing-cast"),
        )
        with pytest.raises(StateMachineError) as conflict:
            sm.submit_command(changed, package, context=context)
        assert conflict.value.code == "idempotency_conflict"
        assert sm.submit_command(confirmation, package, context=context) == outcome


def test_public_spell_data_adapter_does_not_accept_willingness(multi_world):
    sm, _, _, _ = multi_world
    with sm:
        command, _ = _initial(multi_world)
        data = asdict(command)
        data.pop("check_world_effects")
        data["payload"] = command.payload.model_dump(mode="json")
        assert world_command_from_data(data) == command
        data["payload"]["willing_target_ids"] = [NPC, OTHER_ENEMY]
        with pytest.raises(StateMachineError):
            world_command_from_data(data)
