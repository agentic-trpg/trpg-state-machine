"""Public JSON condition choice for an actual bound Lesser Restoration cast."""

import asyncio
import json
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_state import ConditionRecord
from item_support import NPC
from spatial_support import submit as submit_world
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    AdvanceSessionTime,
    StateMachine,
    StateMachineError,
    world_command_from_data,
)


def _condition(name, source):
    """Seed one independently owned condition with stable source fields."""
    return ConditionRecord(
        condition=name,
        source_entity_id=source,
        scope="session",
        duration_rounds=None,
        save_dc=None,
        applied_round=0,
        exhaustion_level=1,
        source_effect_id=None,
    ).model_dump(mode="json")


def test_public_condition_choice_preserves_other_conditions_and_retry(timed_spell_world, tmp_path):
    """An authorized real cast pays once and survives a reopened receipt retry."""
    original = [
        _condition("poisoned", "implied:poison"),
        _condition("deafened", "implied:noise"),
    ]

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        for foundation in data["rule_bindings"].values():
            foundation["snapshot"]["character_states"][0]["conditions"] = original

    session = timed_spell_world(
        additional_spells=("lesser-restoration",),
        caster_class="cleric",
        caster_subclass="life",
        configure=configure,
        database="condition-choice.sqlite",
    )
    sm, sid, package, context = session
    with sm:
        command = cast_command(
            session, spell_id="lesser-restoration", slot_level=2, source_id="class:cleric"
        )
        raw = {
            "session_id": sid,
            "command_id": command.command_id,
            "actor_id": command.actor_id,
            "command_type": command.command_type,
            "expected_world_version": command.expected_world_version,
            "schema_version": command.schema_version,
            "payload": {
                **command.payload.model_dump(mode="json"),
                "selected_condition": "poisoned",
            },
        }
        selected = world_command_from_data(json.loads(json.dumps(raw)))
        before_rng = sm.read_rng_context(sid, package, context=context)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        before_ids = {
            row["source_entity_id"]: row["condition_id"]
            for row in sm._conn.execute(
                "SELECT source_entity_id,condition_id FROM character_conditions "
                "WHERE session_id=? AND actor_id=?",
                (sid, command.actor_id),
            )
        }
        receipt = sm.submit_command(selected, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        actor = next(a for a in after.character_states if a.entity_id == command.actor_id)
        assert [(c.condition, c.source_entity_id) for c in actor.conditions] == [
            ("deafened", "implied:noise")
        ]
        after_ids = {
            row["source_entity_id"]: row["condition_id"]
            for row in sm._conn.execute(
                "SELECT source_entity_id,condition_id FROM character_conditions "
                "WHERE session_id=? AND actor_id=?",
                (sid, command.actor_id),
            )
        }
        assert after_ids == {
            name: cid for name, cid in before_ids.items() if name != "implied:poison"
        }
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert next(p.current for p in after.resource_state.pools if p.pool_id == "spell:2") == (
            next(p.current for p in before.resource_state.pools if p.pool_id == "spell:2") - 1
        )
        assert sm.submit_command(selected, package, context=context) == receipt
        loader = sm._characters.loader

    with StateMachine(tmp_path / "condition-choice.sqlite", dnd_loader=loader) as reopened:
        assert reopened.submit_command(selected, package, context=context) == receipt
        after = reopened.read_noncombat_snapshot(sid, package, context=context)
        actor = next(a for a in after.character_states if a.entity_id == command.actor_id)
        assert [(c.condition, c.source_entity_id) for c in actor.conditions] == [
            ("deafened", "implied:noise")
        ]


@pytest.mark.parametrize("exact", [False, True])
def test_selected_condition_and_spell_cast_ending_commit_together(timed_spell_world, exact):
    """Both condition selectors compose with the authorized end-of-spell delta."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
        snapshot["character_states"][0]["conditions"] = [_condition("poisoned", "implied:toxin")]

    session = timed_spell_world(
        additional_spells=("lesser-restoration", "invisibility"),
        caster_class="bard",
        caster_subclass="lore",
        configure=configure,
    )
    sm, sid, package, context = session
    with sm:
        hidden = cast_command(
            session, spell_id="invisibility", slot_level=2, source_id="class:bard"
        )
        assert sm.submit_command(hidden, package, context=context).status == "committed"
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        assert any(effect.id == "effect:invisibility" for effect in before.effect_states)
        restore = cast_command(
            session, spell_id="lesser-restoration", slot_level=2, source_id="class:bard"
        )
        selector = {"selected_condition": "poisoned"}
        if exact:
            choices = asyncio.run(
                sm.query_spell_condition_choices(restore, package, context=context)
            )
            (option,) = choices.options
            selector = {"selected_condition_id": option.condition_id}
        raw = {
            "session_id": sid,
            "command_id": restore.command_id,
            "actor_id": restore.actor_id,
            "command_type": restore.command_type,
            "expected_world_version": restore.expected_world_version,
            "schema_version": restore.schema_version,
            "payload": {
                **restore.payload.model_dump(mode="json"),
                **selector,
            },
        }
        command = world_command_from_data(json.loads(json.dumps(raw)))
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert not after.effect_states
        assert not next(
            a for a in after.character_states if a.entity_id == restore.actor_id
        ).conditions
        (ending,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs and event.event_type == "effect.spell_ended"
        ]
        assert ending.payload["cast_command_id"] == hidden.command_id
        assert ending.payload["reason"] == "spell_cast"
        assert sm.submit_command(command, package, context=context) == receipt


def test_invalid_or_stale_condition_choice_has_no_payment(timed_spell_world):
    """An ineligible choice and stale expected version leave the slot untouched."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        for foundation in data["rule_bindings"].values():
            foundation["snapshot"]["character_states"][0]["conditions"] = [
                _condition("poisoned", "implied:poison")
            ]

    session = timed_spell_world(
        additional_spells=("lesser-restoration",),
        caster_class="cleric",
        caster_subclass="life",
        configure=configure,
    )
    sm, sid, package, context = session
    with sm:
        command = cast_command(
            session, spell_id="lesser-restoration", slot_level=2, source_id="class:cleric"
        )
        old = sm.read_noncombat_snapshot(sid, package, context=context)
        rejected = sm.submit_command(
            replace(
                command,
                payload=command.payload.model_copy(update={"selected_condition": "stunned"}),
            ),
            package,
            context=context,
        )
        assert rejected.status == "rejected", rejected
        new = sm.read_noncombat_snapshot(sid, package, context=context)
        assert new.resource_state == old.resource_state
        assert new.character_states == old.character_states
        assert (
            submit_world(sm, sid, package, context, AdvanceSessionTime(1), "tick").status
            == "committed"
        )
        stale = sm.submit_command(
            replace(
                command,
                command_id="stale-choice",
                payload=command.payload.model_copy(update={"selected_condition": "poisoned"}),
            ),
            package,
            context=context,
        )
        assert stale.status == "conflict", stale
        latest = sm.read_noncombat_snapshot(sid, package, context=context)
        assert latest.resource_state == old.resource_state
        assert [
            c.condition
            for c in next(
                a for a in latest.character_states if a.entity_id == command.actor_id
            ).conditions
        ] == ["poisoned"]


def test_duplicate_independent_name_requires_source_bound_choice(timed_spell_world):
    """LF1122-F40-01 refuses a name that cannot identify one authoritative source."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][0][
            "conditions"
        ] = [
            _condition("poisoned", "implied:poison"),
            _condition("poisoned", "implied:toxin"),
        ]

    session = timed_spell_world(
        additional_spells=("lesser-restoration",),
        caster_class="cleric",
        caster_subclass="life",
        configure=configure,
    )
    sm, sid, package, context = session
    with sm:
        command = cast_command(
            session, spell_id="lesser-restoration", slot_level=2, source_id="class:cleric"
        )
        command = replace(
            command, payload=command.payload.model_copy(update={"selected_condition": "poisoned"})
        )
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == (
            "rejected",
            "spell.condition_choice_ambiguous",
            (),
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng


def test_condition_write_failure_rolls_back_payment_and_receipt(timed_spell_world, monkeypatch):
    """A failure after the proposed slot debit leaves no partial mechanical success."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][0][
            "conditions"
        ] = [_condition("poisoned", "implied:poison")]

    session = timed_spell_world(
        additional_spells=("lesser-restoration",),
        caster_class="cleric",
        caster_subclass="life",
        configure=configure,
    )
    sm, sid, package, context = session
    with sm:
        command = cast_command(
            session, spell_id="lesser-restoration", slot_level=2, source_id="class:cleric"
        )
        command = replace(
            command, payload=command.payload.model_copy(update={"selected_condition": "poisoned"})
        )
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm.read_events(sid, package, context=context)
        original = sm._characters.update_rule_metadata

        def fail_condition(sid, actor_id, field, value):
            if field == "conditions":
                raise RuntimeError("injected condition write failure")
            return original(sid, actor_id, field, value)

        with monkeypatch.context() as patch:
            patch.setattr(sm._characters, "update_rule_metadata", fail_condition)
            with pytest.raises(RuntimeError, match="injected condition write failure"):
                sm.submit_command(command, package, context=context)
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_events(sid, package, context=context) == before_events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sm.submit_command(command, package, context=context).status == "committed"


@pytest.mark.parametrize("second_origin", ["implied:toxin", "implied:venom"])
def test_caster_selects_exact_other_actor_condition(
    timed_spell_world, tmp_path, monkeypatch, second_origin
):
    """A public cast removes the specified row even when its neighbor has equal facts."""
    poisoned = _condition("poisoned", "implied:toxin")
    other = _condition("poisoned", second_origin)
    deafened = _condition("deafened", "implied:noise")
    stunned = _condition("stunned", "implied:stun")

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        pc, npc = data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"]
        pc["conditions"] = [poisoned]
        npc["conditions"] = [poisoned, other, deafened, stunned]

    session = timed_spell_world(
        additional_spells=("lesser-restoration",),
        caster_class="cleric",
        caster_subclass="life",
        configure=configure,
        database="exact-condition.sqlite",
    )
    sm, sid, package, context = session
    with sm:
        evaluate = sm._rules._evaluate_dnd

        async def evaluate_wire(request):
            """Use the real closed RE wire before evaluating the fixed inputs."""
            return await evaluate(
                RuleEvaluationRequest.model_validate_json(request.model_dump_json())
            )

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", evaluate_wire)
        seed = submit_world(sm, sid, package, context, AdvanceSessionTime(1), "seed-time")
        assert seed.status == "committed", seed
        query = cast_command(
            session,
            spell_id="lesser-restoration",
            slot_level=2,
            source_id="class:cleric",
            target_id=NPC,
        )
        unauthorized = replace(context, principal_id="unrelated-principal")
        with pytest.raises(StateMachineError) as denied:
            asyncio.run(sm.query_spell_condition_choices(query, package, context=unauthorized))
        assert denied.value.code == "unauthorized"
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm.read_events(sid, package, context=context)
        choices = asyncio.run(sm.query_spell_condition_choices(query, package, context=context))
        assert choices.world_version == before.world_version
        first, second, remaining = choices.options
        assert [option.condition for option in choices.options] == [
            "poisoned",
            "poisoned",
            "deafened",
        ]
        assert len({option.condition_id for option in choices.options}) == 3
        assert all(
            set(asdict(option)) == {"condition_id", "condition"} for option in choices.options
        )
        assert (
            asyncio.run(sm.query_spell_condition_choices(query, package, context=context))
            == choices
        )
        wrong_target = replace(
            query, payload=query.payload.model_copy(update={"target_id": "missing-creature"})
        )
        assert not asyncio.run(
            sm.query_spell_condition_choices(wrong_target, package, context=context)
        ).options
        wrong_slot = replace(query, payload=query.payload.model_copy(update={"slot_level": 3}))
        assert not asyncio.run(
            sm.query_spell_condition_choices(wrong_slot, package, context=context)
        ).options
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_events(sid, package, context=context) == before_events

        def exact(command_id, condition_id=first.condition_id, **changes):
            payload = {
                **query.payload.model_dump(mode="json"),
                "selected_condition_id": condition_id,
            }
            raw = {
                "session_id": sid,
                "command_id": command_id,
                "actor_id": query.actor_id,
                "command_type": query.command_type,
                "expected_world_version": choices.world_version,
                "schema_version": query.schema_version,
                "payload": {**payload, **changes},
            }
            return world_command_from_data(json.loads(json.dumps(raw)))

        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(exact("not-authorized"), package, context=unauthorized)
        assert denied.value.code == "unauthorized"
        with pytest.raises(StateMachineError) as dual_selector:
            exact("two-selectors", selected_condition="poisoned")
        assert dual_selector.value.code == "invalid_command"
        wrong = sm.submit_command(
            exact("wrong-target", target_id=query.actor_id), package, context=context
        )
        assert (wrong.status, wrong.error_code, wrong.event_seqs) == (
            "rejected",
            "spell.condition_choice",
            (),
        )
        stale = sm.submit_command(
            replace(exact("old-version"), expected_world_version=choices.world_version - 1),
            package,
            context=context,
        )
        assert (stale.status, stale.error_code, stale.event_seqs) == (
            "conflict",
            "version_conflict",
            (),
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng

        # Remove the first duplicate. A value-only list rewrite would retain it
        # and remove the second when the two rows have identical source facts.
        selected = exact("exact-poison")
        remove = sm._characters.remove_condition

        def fail_after_delete(sid, actor, condition_id):
            assert remove(sid, actor, condition_id)
            raise RuntimeError("injected failure after exact deletion")

        with monkeypatch.context() as patch:
            patch.setattr(sm._characters, "remove_condition", fail_after_delete)
            with pytest.raises(RuntimeError, match="injected failure"):
                sm.submit_command(selected, package, context=context)
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_events(sid, package, context=context) == before_events
        assert sm.get_command_receipt(sid, selected.command_id, package, context=context) is None

        receipt = sm.submit_command(selected, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        cured = next(actor for actor in after.character_states if actor.entity_id == NPC)
        assert [c.model_dump(mode="json") for c in cured.conditions] == [other, deafened, stunned]
        assert next(p.current for p in after.resource_state.pools if p.pool_id == "spell:2") == (
            next(p.current for p in before.resource_state.pools if p.pool_id == "spell:2") - 1
        )
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        current = asyncio.run(sm.query_spell_condition_choices(query, package, context=context))
        assert current.options == (second, remaining)
        assert current.world_version == after.world_version
        expired = sm.submit_command(
            replace(exact("removed-id"), expected_world_version=current.world_version),
            package,
            context=context,
        )
        assert (expired.status, expired.error_code, expired.event_seqs) == (
            "rejected",
            "spell.condition_choice",
            (),
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context) == after
        loader = sm._characters.loader

    with StateMachine(tmp_path / "exact-condition.sqlite", dnd_loader=loader) as reopened:
        assert reopened.submit_command(selected, package, context=context) == receipt
        assert reopened.read_noncombat_snapshot(sid, package, context=context) == after
