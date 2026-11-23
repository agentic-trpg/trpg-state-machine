"""Public held-die exploration choice, fixed original roll and atomic recovery."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect
from dnd_support import NPC, create, initialize
from modern_support import modern_data, modern_loader
from test_real_checks import check_command
from test_scoped_runtime_validation import measure

from trpg_state_machine import AdvanceSessionTime, StateMachine, StateMachineError, TimeBinding
from trpg_state_machine.choice_models import ChoiceRespondOperationPayload
from trpg_state_machine.package import _validate
from trpg_state_machine.pending_bardic_checks import resolve_bardic_check_choice

EFFECT = "effect:inspired"
ORIGIN = f"cast:inspired:{NPC}"
DEADLINE = 3600


@pytest.fixture
def table(tmp_path, package_data, context):
    return make_table(tmp_path, package_data, context)


def make_table(tmp_path, package_data, context, *, growth=0):
    assets = modern_loader()
    data = modern_data(package_data, assets)
    data["time_binding"] = asdict(TimeBinding())
    remote = data["scenes"][1]
    for i in range(growth):
        actor = f"npc.remote_{i}"
        data["actors"].append({**deepcopy(data["actors"][1]), "actor_id": actor})
        remote["actor_ids"].append(actor)
        remote["objects"].append({"object_id": f"object.remote_{i}", "state": "closed"})
    package = _validate(data)
    context = replace(context, can_adjudicate_checks=True, can_advance_time=True)
    db = tmp_path / f"bardic-{growth}.sqlite"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
    return db, assets, sid, package, context


def grant(sm, sid, actor, key="grant.original"):
    inspired = ActiveEffect(
        id=EFFECT,
        name="Inspired",
        origin=ORIGIN,
        target_id=actor,
        flags={"bardic_inspiration": {"grant_id": key, "die_sides": 6}},
    )
    with sm._store.transaction(write=True):
        sm._world.characters.effects.add_effect(
            sid, effect_state(inspired).model_dump(mode="json"), expires_tick=DEADLINE
        )


def current_command(sm, sid, package, context, key, payload, *, actor=None):
    state = sm.read_session(sid, package, context=context)
    return replace(
        check_command(sm, sid, package, context, key=key),
        actor_id=actor or state.pc_actor_id,
        command_type=(
            "choice.respond"
            if isinstance(payload, ChoiceRespondOperationPayload)
            else "session.advance_time"
        ),
        payload=payload,
    )


def reply(sm, sid, package, context, key, original, option="accept", *, actor=None):
    return current_command(
        sm,
        sid,
        package,
        context,
        key,
        ChoiceRespondOperationPayload(original.command_id, option),
        actor=actor,
    )


def held(sm, sid, actor):
    with sm._store.transaction():
        return sm._world.characters.effects.for_target_effect(sid, actor, EFFECT)


def latest(sm, sid, package, context):
    return sm.read_events(sid, package, context=context)[-1]


def open_choice(sm, sid, package, context, *, dc=99):
    source = check_command(sm, sid, package, context, key="initial-check", dc=dc)
    grant(sm, sid, source.actor_id)
    receipt = sm.submit_command(source, package, context=context)
    assert receipt.status == "pending_choice", receipt
    assert receipt.pending_choice.choice_kind == "rules.bardic_inspiration"
    assert receipt.pending_choice.pending_operation_id == source.command_id
    assert receipt.pending_choice.authorized_responder == source.actor_id
    assert receipt.pending_choice.expires_at_tick == DEADLINE
    return source, receipt


@pytest.mark.parametrize(("option", "dc"), [("accept", 19), ("accept", 99), ("decline", 99)])
def test_bardic_durable_original_and_current_rng(table, option, dc, monkeypatch):
    db, assets, sid, package, gm = table
    player = replace(gm, can_adjudicate_checks=False)
    with StateMachine(db, dnd_loader=assets) as sm:
        initial_rng = sm.read_rng_context(sid, package, context=player)
        source, opened = open_choice(sm, sid, package, gm, dc=dc)
        prefix = latest(sm, sid, package, gm)
        assert prefix.event_type == "rules.bardic_check_pending" and not prefix.payload
        assert not prefix.signals
        assert prefix.event_seq in opened.event_seqs
        projection = sm.get_command_rolls(sid, source.command_id, package, context=player)
        assert projection["status"] == "pending_choice"
        original = projection["rolls"][0]
        assert original["event_seq"] == prefix.event_seq
        candidate = initial_rng.state.restore()
        assert original["natural"] == candidate.randint(1, 20)
        assert original["succeeded"] is False
        assert held(sm, sid, source.actor_id)
        assert sm.read_rng_context(sid, package, context=player).state == RNGState.capture(
            candidate
        )
        assert sm.submit_command(source, package, context=gm) == opened
        advance = current_command(sm, sid, package, gm, "advance-clock", AdvanceSessionTime(10))
        assert sm.submit_command(advance, package, context=gm).status == "committed"
        assert (
            sm.submit_command(
                check_command(sm, sid, package, gm, key="unrelated-d20", dc=1), package, context=gm
            ).status
            == "committed"
        )
        assert latest(sm, sid, package, gm).event_type == "rules.check_rolled"
        rng = sm.read_rng_context(sid, package, context=player)
        assert rng.version == initial_rng.version + 2

    with StateMachine(db, dnd_loader=assets) as sm:
        sm.resume_session(sid, package, context=player)
        used = []

        def observed(original, sides, context, *, use):
            assert not sm._conn.in_transaction
            used.append((sides, context, use))
            return resolve_bardic_check_choice(original, sides, context, use=use)

        monkeypatch.setattr(
            "trpg_state_machine.pending_bardic_checks.resolve_bardic_check_choice", observed
        )
        answer = reply(sm, sid, package, player, "answer", source, option)
        assert answer.expected_world_version > opened.world_version
        receipt = sm.submit_command(answer, package, context=player)
        assert receipt.status == "committed", receipt
        final = latest(sm, sid, package, player)
        assert final.event_type == "rules.check_rolled" and not final.signals
        assert final.payload["original_command_id"] == source.command_id
        roll = final.payload["engine_event"]
        assert roll["natural"] == original["natural"]
        assert roll["roll_detail"]["d20"] == original["roll_detail"]["d20"]
        assert roll["modifier"] == original["modifier"]
        next_rng = sm.read_rng_context(sid, package, context=player)
        if option == "accept":
            stream = rng.state.restore()
            bonus = stream.randint(1, 6)
            assert roll["inspiration_bonus"] == bonus
            assert roll["roll_total"] == original["roll_total"] + bonus
            assert roll["succeeded"] is (dc == 19)
            assert roll["roll_detail"]["modifiers"][:-1] == original["roll_detail"]["modifiers"]
            assert (
                next_rng.state == RNGState.capture(stream) and next_rng.version == rng.version + 1
            )
            assert not held(sm, sid, source.actor_id)
        else:
            final_projection = sm.get_command_rolls(sid, answer.command_id, package, context=player)
            assert final_projection["rolls"] == [{**original, "event_seq": final.event_seq}]
            assert next_rng == rng
            assert held(sm, sid, source.actor_id)
        assert used == [(6, rng, option == "accept")]
        assert sm.submit_command(answer, package, context=player) == receipt
        assert sm.submit_command(source, package, context=gm) == opened
        later = reply(sm, sid, package, player, "second-answer", source)
        assert sm.submit_command(later, package, context=player).error_code == "choice_closed"
        assert len(used) == 1


@pytest.mark.parametrize("change", ["expiry", "granted_again", "check_modifier", "combat"])
def test_bardic_revalidates_exact_current_dependencies(table, change):
    db, assets, sid, package, context = table
    with StateMachine(db, dnd_loader=assets) as sm:
        source, _ = open_choice(sm, sid, package, context)
        bad_actor = reply(sm, sid, package, context, "wrong-actor", source, actor=NPC)
        with pytest.raises(StateMachineError, match="Choice belongs to another actor"):
            sm.submit_command(bad_actor, package, context=context)
        if change == "expiry":
            advance = current_command(
                sm, sid, package, context, "expire-grant", AdvanceSessionTime(DEADLINE)
            )
            assert sm.submit_command(advance, package, context=context).status == "committed"
            assert not held(sm, sid, source.actor_id)
        elif change == "granted_again":
            with sm._store.transaction(write=True):
                sm._world.characters.effects.remove_effect(sid, source.actor_id, EFFECT, ORIGIN)
            grant(sm, sid, source.actor_id, "grant.replacement")
        elif change == "check_modifier":
            with sm._store.transaction(write=True):
                sm._world.characters.add_condition(
                    sid,
                    source.actor_id,
                    ActiveCondition(
                        condition="poisoned", source_entity_id="implied:poisoned", scope="session"
                    ),
                )
        else:
            assert initialize(sm, sid, package, context).status == "committed"
        before = sm.read_rng_context(sid, package, context=context)
        last = latest(sm, sid, package, context)
        answer = reply(sm, sid, package, context, "first-answer", source)
        rejected = sm.submit_command(answer, package, context=context)
        assert rejected.status == "rejected" and not rejected.event_seqs
        assert rejected.error_code == ("choice_expired" if change == "expiry" else "choice_stale")
        assert latest(sm, sid, package, context) == last
        original_projection = sm.get_command_rolls(sid, source.command_id, package, context=context)
        assert original_projection["status"] == "pending_choice"
        assert original_projection["rolls"][0]["succeeded"] is False
        assert sm.read_rng_context(sid, package, context=context) == before
        assert sm.submit_command(answer, package, context=context) == rejected
        assert bool(held(sm, sid, source.actor_id)) is (change != "expiry")


@pytest.mark.parametrize("on_reply", [False, True])
def test_bardic_late_receipt_failure_rolls_back_stage_and_allows_retry(
    table, on_reply, monkeypatch
):
    db, assets, sid, package, context = table
    with StateMachine(db, dnd_loader=assets) as sm:
        if on_reply:
            source, _ = open_choice(sm, sid, package, context)
            command = reply(sm, sid, package, context, "answer", source)
        else:
            command = check_command(sm, sid, package, context, dc=99)
            grant(sm, sid, command.actor_id)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        grant_before = held(sm, sid, command.actor_id)
        stored = sm._session_runtime.record_receipt

        def failure_after_receipt(*args):
            stored(*args)
            raise RuntimeError("late Bardic receipt failure")

        with monkeypatch.context() as patch:
            patch.setattr(sm._session_runtime, "record_receipt", failure_after_receipt)
            with pytest.raises(RuntimeError, match="late Bardic receipt failure"):
                sm.submit_command(command, package, context=context)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sm.read_events(sid, package, context=context) == events
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert held(sm, sid, command.actor_id) == grant_before
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == ("committed" if on_reply else "pending_choice")
        assert sm.read_rng_context(sid, package, context=context).version == rng.version + 1


def test_bardic_invalid_option_and_foreign_identity_leave_window_untouched(table):
    db, assets, sid, package, context = table
    with StateMachine(db, dnd_loader=assets) as sm:
        source, _ = open_choice(sm, sid, package, context)
        rogue = replace(context, principal_id="authenticated:someone-else")
        response = reply(sm, sid, package, context, "choose", source)
        expected = sm.read_rng_context(sid, package, context=context)
        with pytest.raises(StateMachineError) as invalid:
            sm.submit_command(
                replace(
                    response, payload=ChoiceRespondOperationPayload(source.command_id, "force")
                ),
                package,
                context=context,
            )
        assert invalid.value.code == "invalid_operation"
        for denied in (
            lambda: sm.submit_command(response, package, context=rogue),
            lambda: sm.get_command_rolls(sid, source.command_id, package, context=rogue),
        ):
            with pytest.raises(StateMachineError) as unauthorized:
                denied()
            assert unauthorized.value.code == "unauthorized"
        assert sm.get_command_receipt(sid, response.command_id, package, context=context) is None
        assert sm.read_rng_context(sid, package, context=context) == expected
        assert held(sm, sid, source.actor_id)
        assert sm.submit_command(response, package, context=context).status == "committed"


def test_bardic_raw_event_and_delivery_omit_pending_roll_and_adjudication(table):
    db, assets, sid, package, gm = table
    player = replace(gm, can_adjudicate_checks=False)
    with StateMachine(db, dnd_loader=assets) as sm:
        source, opened = open_choice(sm, sid, package, gm)
        marker = latest(sm, sid, package, player)
        assert (marker.event_type, marker.payload, marker.signals) == (
            "rules.bardic_check_pending",
            {},
            (),
        )
        delivered = []
        assert sm.deliver_event(
            sid, marker.event_seq, package, context=player, sink=delivered.append
        )
        assert delivered == [marker]
        projection = sm.get_command_rolls(sid, source.command_id, package, context=player)
        assert projection["status"] == opened.status == "pending_choice"
        original = projection["rolls"][0]
        assert original["succeeded"] is False and original["roll_detail"]["d20"]["faces"]
        assert "adjudication" not in original

        answer = reply(sm, sid, package, player, "final-answer", source, "decline")
        resolved = sm.submit_command(answer, package, context=player)
        final = latest(sm, sid, package, player)
        assert final.event_seq in resolved.event_seqs
        assert final.event_type == "rules.check_rolled"
        assert final.payload["engine_event"]["roll_detail"] == original["roll_detail"]
        assert final.payload["adjudication"]["payload"] == source.payload.model_dump(mode="json")
        assert sm.deliver_event(
            sid, final.event_seq, package, context=player, sink=delivered.append
        )
        assert delivered == [marker, final]
        assert sm.get_command_rolls(sid, source.command_id, package, context=player) == projection


def test_bardic_single_answer_overlapping_another_connection(table, monkeypatch):
    db, assets, sid, package, context = table
    with StateMachine(db, dnd_loader=assets) as sm:
        source, _ = open_choice(sm, sid, package, context)
        before = sm.read_rng_context(sid, package, context=context)
        accept = reply(sm, sid, package, context, "accept", source)
        decline = reply(sm, sid, package, context, "decline", source, "decline")

        def submit_decline():
            with StateMachine(db, dnd_loader=assets) as competitor:
                return competitor.submit_command(decline, package, context=context)

        def intervene(original, sides, rng, *, use):
            if use:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    won = executor.submit(submit_decline).result(timeout=15)
                    assert won.status == "committed", won
            return resolve_bardic_check_choice(original, sides, rng, use=use)

        monkeypatch.setattr(
            "trpg_state_machine.pending_bardic_checks.resolve_bardic_check_choice", intervene
        )
        lost = sm.submit_command(accept, package, context=context)
        assert lost.error_code == "choice_closed" and lost.event_seqs == ()
        assert sm.read_rng_context(sid, package, context=context) == before
        assert held(sm, sid, source.actor_id)
        assert latest(sm, sid, package, context).source_command_id == decline.command_id


def test_bardic_reply_read_cost_is_fixed_with_unrelated_actors_and_objects(
    tmp_path, package_data, context
):
    observations = []
    for growth in (0, 25):
        db, assets, sid, package, gm = make_table(tmp_path, package_data, context, growth=growth)
        with StateMachine(db, dnd_loader=assets) as sm:
            source, _ = open_choice(sm, sid, package, gm)
            response = reply(sm, sid, package, gm, "answer", source)
            with measure(sm) as (counts, statements):
                assert sm.submit_command(response, package, context=gm).status == "committed"
            assert all("WHERE session_id=? AND current_scene_id=?" not in sql for sql in statements)
            observations.append(
                tuple(counts[key] for key in ("sql_selects", "sql_rows", "returned_bytes"))
            )
    assert observations[0] == observations[1]


def test_bardic_requires_held_die_and_actual_failure(table, monkeypatch):
    db, assets, sid, package, context = table

    def unexpected_fingerprint(*args):
        raise AssertionError("No held die after a failed check")

    monkeypatch.setattr(
        "trpg_state_machine.pending_bardic_checks.check_bardic_fingerprint", unexpected_fingerprint
    )
    with StateMachine(db, dnd_loader=assets) as sm:
        no_die = check_command(sm, sid, package, context, key="no-die", dc=99)
        assert sm.submit_command(no_die, package, context=context).status == "committed"
        assert latest(sm, sid, package, context).payload["engine_event"]["succeeded"] is False
        grant(sm, sid, no_die.actor_id)
        successful = check_command(sm, sid, package, context, key="succeeded", dc=1)
        assert sm.submit_command(successful, package, context=context).status == "committed"
        assert latest(sm, sid, package, context).payload["engine_event"]["succeeded"] is True
        assert held(sm, sid, no_die.actor_id)


def test_bardic_rejects_invalid_original_result_before_saving(table, monkeypatch):
    db, assets, sid, package, gm = table
    with StateMachine(db, dnd_loader=assets) as sm:
        source = check_command(sm, sid, package, gm, dc=99)
        grant(sm, sid, source.actor_id)
        before = sm.read_rng_context(sid, package, context=gm)
        events = sm.read_events(sid, package, context=gm)
        evaluate = sm._rules._evaluate_dnd

        async def extra_event(request):
            result = await evaluate(request)
            return result.model_copy(update={"proposed_events": result.proposed_events * 2})

        with monkeypatch.context() as patch:
            patch.setattr(sm._rules, "_evaluate_dnd", extra_event)
            with pytest.raises(StateMachineError) as invalid:
                sm.submit_command(source, package, context=gm)
        assert invalid.value.code == "invalid_rule_result"
        assert sm.read_events(sid, package, context=gm) == events
        assert sm.read_rng_context(sid, package, context=gm) == before
        assert sm.get_command_receipt(sid, source.command_id, package, context=gm) is None
        assert held(sm, sid, source.actor_id)
        assert sm.submit_command(source, package, context=gm).status == "pending_choice"
