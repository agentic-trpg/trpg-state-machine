"""Exercise the public NPC attack and same-process Shield answer."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.activities import attack as attack_activity
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_shield import shield_graph, shield_source_id
from dnd5e_engine.evaluation_shield_delta import ShieldStateTransition
from dnd_support import initialize
from item_support import NPC, item_data
from test_npc_evaluation_tasks import create_task
from test_timed_spell_cast import OTHER_ENEMY, cast_command, first_level_balance
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    ChoiceRespondOperationPayload,
    StateMachineError,
    TypedCommand,
    shield_window,
)
from trpg_state_machine.npc_results import NPCCombatIntent, NPCEvaluationResult
from trpg_state_machine.perception import NPCContext

LATER_NPC = "npc.later"


def _prepare(
    timed_spell_world,
    *,
    immediate_expiry=False,
    extra_before_owner=False,
    legacy_clock=False,
    hp=None,
    **options,
):
    """Place the bound Ogre adjacent to a Wizard with a prepared Shield."""

    def adjacent(data):
        """Move the attacker and its authored mechanical placement together."""
        data["actors"][0]["initial_cell"] = "1,0"
        combat = data["rule_bindings"]["combat_foundation"]["snapshot"]["combat_state"]
        combat["actor_zone"][NPC] = "1,0"
        pc = data["pc_templates"][0]["pc_template_id"]
        if hp is not None:
            data["pc_templates"][0]["character"]["hp"] = {"current": hp[0], "max": hp[1]}
            for foundation in data["rule_bindings"].values():
                actor = next(
                    a for a in foundation["snapshot"]["character_states"] if a["entity_id"] == pc
                )
                actor.update(hp_current=hp[0], hp_max=hp[1])
        combat["initiative_ids"] = (
            [NPC, pc, OTHER_ENEMY] if immediate_expiry else [NPC, OTHER_ENEMY, pc]
        )
        if legacy_clock:
            data.pop("time_binding")
            data["actors"][0]["npc_state"] = item_data()["actors"][0]["npc_state"]
        if extra_before_owner:
            other = deepcopy(data["actors"][1])
            other.update(actor_id=LATER_NPC, name="Later NPC", initial_cell="3,1")
            data["actors"].append(other)
            data["scenes"][0]["actor_ids"].append(LATER_NPC)
            data["navigation"]["actor_grants"].append(
                {"actor_id": LATER_NPC, "region_ids": ["region.public"]}
            )
            for foundation in data["rule_bindings"].values():
                snapshot = foundation["snapshot"]
                state = next(
                    a for a in snapshot["character_states"] if a["entity_id"] == OTHER_ENEMY
                )
                snapshot["character_states"].append(
                    {**deepcopy(state), "entity_id": LATER_NPC, "name": "Later NPC"}
                )
                if "combat_state" in snapshot:
                    combat["initiative_ids"].insert(2, LATER_NPC)
                    combat["encounter_ids"].append(LATER_NPC)
                    combat["actor_zone"][LATER_NPC] = "3,1"
                    combat["movement_ledgers"][LATER_NPC] = deepcopy(
                        combat["movement_ledgers"][OTHER_ENEMY]
                    )
                    combat["xp_value_by_entity"][LATER_NPC] = 0
                else:
                    foundation["xp"][LATER_NPC] = 0

    return timed_spell_world(
        walking=True,
        attack_after_walk=True,
        hit_rider="ray-of-frost",
        additional_spells=("shield",),
        configure=adjacent,
        **options,
    )


@pytest.mark.parametrize(
    ("option", "seed", "natural", "immediate_expiry"),
    [
        ("accept", 3, 8, False),
        ("decline", 3, 8, False),
        ("accept", 5, 20, False),
        ("accept", 3, 8, True),
    ],
)
def test_public_shield_response(
    timed_spell_world, option, seed, natural, immediate_expiry, monkeypatch
):
    sm, sid, package, context = _prepare(
        timed_spell_world, seed=seed, immediate_expiry=immediate_expiry, hp=(80, 100)
    )
    with sm:
        defender_context = replace(context, controlled_npc_ids=())
        assert initialize(sm, sid, package, context).status == "committed"
        cast = cast_command((sm, sid, package, context))
        pc = cast.actor_id

        def snapshot():
            return sm.read_combat_snapshot(sid, package, context=context)

        start = snapshot()
        rng = sm.read_rng_context(sid, package, context=context)
        attack = TypedCommand(
            sid,
            "npc-attack",
            NPC,
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack", target_id=pc, stat_block_action_id="greatclub"
            ),
            start.world_version,
        )

        def unexpected_damage(*args, **kwargs):
            """A pending decision must stop before any damage roll or application."""
            pytest.fail("Shield prompt evaluated damage before the answer")

        with monkeypatch.context() as probe:
            probe.setattr(attack_activity, "_apply_on_hit_damage", unexpected_damage)
            pending = sm.submit_command(attack, package, context=context)
        assert (pending.status, pending.error_code) == ("pending_choice", None), pending
        assert pending.pending_choice.choice_kind == "combat.shield_reaction"
        assert pending.pending_choice.authorized_responder == pc
        assert pending.pending_choice.expires_at_tick is None
        assert set(asdict(pending.pending_choice)) == {
            "pending_operation_id",
            "authorized_responder",
            "choice_kind",
            "allowed_options",
            "expires_at_tick",
        }
        assert pending.event_seqs == ()
        assert snapshot() == start
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.submit_command(attack, package, context=context) == pending
        advance = replace(
            attack, command_id="competing-pass", payload=CombatIntentPayload(intent_type="pass")
        )
        with pytest.raises(StateMachineError, match="Resolve the current combat choice"):
            sm.submit_command(advance, package, context=context)
        answer = TypedCommand(
            sid,
            "shield-answer",
            pc,
            "choice.respond",
            ChoiceRespondOperationPayload(pending.pending_choice.pending_operation_id, option),
            start.world_version,
        )
        with pytest.raises(StateMachineError, match="Choice belongs to another actor"):
            sm.submit_command(replace(answer, actor_id=NPC), package, context=context)
        stale = sm.submit_command(
            replace(answer, command_id="stale-answer", expected_world_version=0),
            package,
            context=context,
        )
        assert (stale.status, stale.error_code) == ("conflict", "version_conflict")
        with monkeypatch.context() as restarted:
            restarted.setattr(shield_window, "process_id", lambda: "foreign-process")
            interrupted = sm.submit_command(
                replace(answer, command_id="interrupted-answer"), package, context=context
            )
        assert (interrupted.status, interrupted.error_code) == ("rejected", "combat_unrestorable")
        if option == "accept":
            stage_outcome = sm._session_runtime.stage_outcome

            def fail_after_staging(*args, **kwargs):
                """Fail after writing the response, RNG, effect, event, and world version."""
                stage_outcome(*args, **kwargs)
                raise RuntimeError("injected Shield response failure")

            with monkeypatch.context() as failure:
                failure.setattr(sm._session_runtime, "stage_outcome", fail_after_staging)
                with pytest.raises(RuntimeError, match="injected Shield response failure"):
                    sm.submit_command(answer, package, context=context)
            assert snapshot() == start
            assert sm.read_rng_context(sid, package, context=context) == rng
            assert first_level_balance(sm, cast) == 4
        receipt = sm.submit_command(answer, package, context=defender_context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(answer, package, context=defender_context) == receipt
        rolls = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs
            and event.payload.get("engine_event", {}).get("type") == "attack_rolled"
        ]
        assert len(rolls) == 1
        assert rolls[0]["natural"] == natural
        assert rolls[0]["is_crit"] is (natural == 20)
        result = snapshot()
        before = next(a for a in start.character_states if a.entity_id == pc)
        after = next(a for a in result.character_states if a.entity_id == pc)
        published_rng = sm.read_rng_context(sid, package, context=context)
        assert published_rng.version == rng.version + 1
        if option == "accept":
            assert first_level_balance(sm, cast) == 3
            if natural == 20:
                assert rolls[0]["is_hit"]
                assert before.hp_current > after.hp_current
                return
            assert not rolls[0]["is_hit"]
            expected_rng = rng.state.restore()
            assert expected_rng.randint(1, 20) == natural
            assert published_rng.state == RNGState.capture(expected_rng)
            assert before.hp_current == after.hp_current
            if immediate_expiry:
                assert after.reaction_available
                assert result.effect_states == ()
                return
            assert not after.reaction_available
            assert len(result.effect_states) == 1
            ac = sm.read_character_ac(sid, pc, package, context=context)
            assert ac.effective == ac.base + 5
            turn = sm.submit_command(
                replace(advance, actor_id=OTHER_ENEMY, expected_world_version=result.world_version),
                package,
                context=context,
            )
            assert turn.status == "committed", turn
            refreshed = snapshot()
            assert refreshed.effect_states == ()
            assert next(
                a for a in refreshed.character_states if a.entity_id == pc
            ).reaction_available
        else:
            assert first_level_balance(sm, cast) == 4
            assert before.hp_current > after.hp_current
            assert after.reaction_available
            assert result.effect_states == ()


@pytest.mark.parametrize(("seed", "slots"), [(1, 4), (3, 0)])
def test_npc_attack_without_eligible_hit_never_pauses(timed_spell_world, seed, slots):
    """A miss or the lack of a usable first-level slot keeps the ordinary command path."""
    sm, sid, package, context = _prepare(timed_spell_world, seed=seed, first_level_slots=slots)
    with sm:
        version = initialize(sm, sid, package, context).world_version
        pc = cast_command((sm, sid, package, context)).actor_id
        command = TypedCommand(
            sid,
            "npc-attack",
            NPC,
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack", target_id=pc, stat_block_action_id="greatclub"
            ),
            version,
        )
        result = sm.submit_command(command, package, context=context)
        assert result.status == "committed"
        assert result.pending_choice is None


def _shield_attack(session):
    """Prepare an adjacent public stat-block attack at the current version."""
    sm, sid, package, context = session
    pc = cast_command(session).actor_id
    snapshot = sm.read_combat_snapshot(sid, package, context=context)
    return TypedCommand(
        sid,
        "npc-attack",
        NPC,
        "combat.intent",
        CombatIntentPayload(intent_type="attack", target_id=pc, stat_block_action_id="greatclub"),
        snapshot.world_version,
    )


def _shield_answer(attack, pending, option="accept"):
    """Use the authorized defender as the answer actor."""
    return TypedCommand(
        attack.session_id,
        "shield-answer",
        attack.payload.target_id,
        "choice.respond",
        ChoiceRespondOperationPayload(pending.pending_choice.pending_operation_id, option),
        pending.world_version,
    )


@pytest.mark.parametrize("fake_death", [False, True])
def test_active_shield_rejects_premature_graph_change(timed_spell_world, monkeypatch, fake_death):
    """An actual handoff to another NPC cannot expire Shield or remove its response."""
    session = _prepare(timed_spell_world, seed=3, extra_before_owner=True)
    sm, sid, package, context = session
    with sm:
        assert initialize(sm, sid, package, context).status == "committed"
        attack = _shield_attack(session)
        pending = sm.submit_command(attack, package, context=context)
        assert (
            sm.submit_command(_shield_answer(attack, pending), package, context=context).status
            == "committed"
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        old = shield_graph(before)
        evaluate = sm._rules._evaluate_dnd

        async def alter_graph(request):
            """Append a schema-valid graph edit to an otherwise genuine handoff."""
            result = await evaluate(request)
            assert result.status == "accepted"
            value = (
                old.model_copy(update={"owner_dead": True, "response": None})
                if fake_death
                else None
            )
            change = ShieldStateTransition(
                kind="combat.shield_state_transition",
                combat_id=before.combat_state.combat_id,
                owner_id=old.expiry_owner_id,
                spell_source_id=shield_source_id(before, old.expiry_owner_id),
                actor_ids=before.combat_state.initiative_ids,
                expected=old,
                value=value,
            )
            delta = result.state_delta
            return result.model_copy(
                update={
                    "state_delta": delta.model_copy(
                        update={"operations": (*delta.operations, change)}
                    )
                }
            )

        handoff = TypedCommand(
            sid,
            "other-pass",
            OTHER_ENEMY,
            "combat.intent",
            CombatIntentPayload(intent_type="pass"),
            before.world_version,
        )
        with monkeypatch.context() as invalid:
            invalid.setattr(sm._rules, "_evaluate_dnd", alter_graph)
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(handoff, package, context=context)
            assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.submit_command(handoff, package, context=context).status == "committed"
        assert (
            sm.read_combat_snapshot(sid, package, context=context).effect_states
            == before.effect_states
        )


@pytest.mark.parametrize("revoke_after_evaluation", [False, True])
def test_revoked_npc_handoff_releases_shield_window(
    timed_spell_world, monkeypatch, revoke_after_evaluation
):
    """A revoked source invalidates the attack while the defender owns the stale reply."""
    session = _prepare(timed_spell_world, seed=3, legacy_clock=True)
    sm, sid, package, context = session
    with sm:
        assert initialize(sm, sid, package, context).status == "committed"
        sm.grant_npc_access(sid, NPC, "agent:ogre", package, context=context)
        npc = NPCContext("agent:ogre", sid, NPC)
        attack = _shield_attack(session)
        task = create_task(sm, sid, package, npc)
        proposal = NPCEvaluationResult(
            action_intents=(NPCCombatIntent(attack.payload, attack.expected_world_version),)
        )
        sm.submit_npc_evaluation_result(task.evaluation_id, proposal, package, context=npc)
        pending = sm.dispatch_npc_output(
            task.evaluation_id, 0, package, context=context
        ).command_receipt
        assert pending.status == "pending_choice", pending
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)

        def revoke():
            """Replace the NPC's controller without a legacy-clock World mutation."""
            sm.grant_npc_access(sid, NPC, "agent:replacement", package, context=context)

        if revoke_after_evaluation:
            from trpg_state_machine import rule_execution

            evaluate = rule_execution.answer_shield

            async def revoke_between_scopes(*args, **kwargs):
                result = await evaluate(*args, **kwargs)
                revoke()
                return result

            monkeypatch.setattr(rule_execution, "answer_shield", revoke_between_scopes)
        else:
            revoke()
        answer = _shield_answer(attack, pending, "decline" if revoke_after_evaluation else "accept")
        with pytest.raises(StateMachineError, match="Choice belongs to another actor"):
            sm.submit_command(replace(answer, actor_id=NPC), package, context=context)
        receipt = sm.submit_command(answer, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "choice_stale")
        assert sm.submit_command(answer, package, context=context) == receipt
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        handoff = replace(
            attack, command_id="next-pass", payload=CombatIntentPayload(intent_type="pass")
        )
        assert sm.submit_command(handoff, package, context=context).status == "committed"


def test_shield_expires_before_the_incoming_dying_defender_saves(timed_spell_world):
    """Continue a real initiative handoff to the dying Shield owner."""
    session = _prepare(timed_spell_world, seed=5, hp=(1, 100))
    sm, sid, package, context = session
    with sm:
        assert initialize(sm, sid, package, context).status == "committed"
        attack = _shield_attack(session)
        pc = attack.payload.target_id
        pending = sm.submit_command(attack, package, context=context)
        accepted = sm.submit_command(_shield_answer(attack, pending), package, context=context)
        assert accepted.status == "committed", accepted
        before = sm.read_combat_snapshot(sid, package, context=context)
        dying = next(actor for actor in before.character_states if actor.entity_id == pc)
        assert dying.hp_current == 0 and dying.is_alive and before.effect_states
        handoff = TypedCommand(
            sid,
            "dying-pass",
            OTHER_ENEMY,
            "combat.intent",
            CombatIntentPayload(intent_type="pass"),
            before.world_version,
        )
        receipt = sm.submit_command(handoff, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert not after.effect_states
        events = tuple(
            event.payload["engine_event"]["type"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs and event.event_type == "combat.engine_event"
        )
        assert events.index("effect_expired") < events.index("death_save_rolled")


def test_unsupported_terminal_shield_accept_keeps_decline_available(timed_spell_world):
    """A death without a future expiry never commits Shield or its payment."""
    session = _prepare(timed_spell_world, seed=5, hp=(1, 1))
    sm, sid, package, context = session
    with sm:
        assert initialize(sm, sid, package, context).status == "committed"
        attack = _shield_attack(session)
        pending = sm.submit_command(attack, package, context=context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        cast = cast_command(session)
        accept = _shield_answer(attack, pending)
        receipt = sm.submit_command(accept, package, context=context)
        assert (receipt.status, receipt.error_code) == (
            "rejected",
            "combat.shield_terminal_unsupported",
        )
        assert sm.submit_command(accept, package, context=context) == receipt
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert first_level_balance(sm, cast) == 4
        decline = replace(_shield_answer(attack, pending, "decline"), command_id="shield-decline")
        settled = sm.submit_command(decline, package, context=context)
        assert settled.status == "committed", settled
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert not after.effect_states
        assert attack.payload.target_id in after.combat_state.dead_ids
        assert first_level_balance(sm, cast) == 4


def test_shield_knockout_with_prior_npc_death_keeps_decline_available(timed_spell_world):
    """A prior terminal NPC record still restricts the existing dying Pass profile."""
    session = _prepare(timed_spell_world, seed=5, hp=(1, 100), extra_before_owner=True)
    sm, sid, package, context = session
    with sm:
        assert initialize(sm, sid, package, context).status == "committed"
        attack = _shield_attack(session)
        initial = sm.read_combat_snapshot(sid, package, context=context)
        previous_victim = next(a for a in initial.character_states if a.entity_id == LATER_NPC)
        # Set up the persisted facts from a prior NPC death without advancing Attack RNG.
        with sm._transaction(write=True):
            sm._characters.set_hp(sid, LATER_NPC, 0)
            sm._characters.update_rule_metadata(sid, LATER_NPC, "is_alive", False)
            sm._combat_records.update_rule_state(sid, "dead_ids", [LATER_NPC])
            sm._combat_records.update_rule_state(
                sid,
                "deaths_recorded",
                [
                    {
                        "target_id": LATER_NPC,
                        "target_kind": previous_victim.entity_type.lower(),
                        "location_id": initial.scene_state.scene_id,
                        "reason": "damage",
                        "killer_id": attack.payload.target_id,
                    }
                ],
            )
        pending = sm.submit_command(attack, package, context=context)
        assert pending.status == "pending_choice", pending
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        cast = cast_command(session)
        accept = _shield_answer(attack, pending)
        receipt = sm.submit_command(accept, package, context=context)
        assert (receipt.status, receipt.error_code) == (
            "rejected",
            "combat.shield_knockout_unsupported",
        )
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert first_level_balance(sm, cast) == 4
        decline = replace(_shield_answer(attack, pending, "decline"), command_id="shield-decline")
        assert sm.submit_command(decline, package, context=context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        owner = next(a for a in after.character_states if a.entity_id == attack.payload.target_id)
        assert owner.hp_current == 0 and owner.is_alive and not after.effect_states
