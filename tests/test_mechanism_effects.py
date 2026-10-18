"""Contract fixtures only. No fixture evaluator is installed by production code."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import ReadVersion, RuleError
from dnd5e_engine.evaluation_delta import HPDelta
from dnd5e_engine.evaluation_rng import RNGTransition
from lock_support import CRED, A
from test_mechanism_service import extended_data, method, trigger
from test_world_mechanisms import LEVER
from world_support import command, setup, submit

from trpg_state_machine import (
    ActivateMechanism,
    CheckMechanism,
    StateMachine,
    StateMachineError,
    StepActor,
)
from trpg_state_machine.mechanism_effects import (
    MechanismAudit,
    MechanismEffectResult,
    UnsupportedMechanismEffects,
    result_envelope,
    validate_mechanism_result,
)
from trpg_state_machine.package import _validate


def effect(**changes):
    value = dict(
        effect_ref="effect.dart",
        schema_version="mechanism-effect-definition/1",
        attack=None,
        save=dict(ability="dex", dc=12, on_success="half_damage"),
        damage=dict(dice="1d4", bonus=0, damage_type="piercing"),
        status=None,
        magical=False,
    )
    value.update(changes)
    return value


def rule_action(**changes):
    selector = dict(
        scope="trigger_actor",
        cells=[],
        actor_kind="all",
        living_only=True,
        exclude_trigger_actor=False,
        selection_time="before_consequence",
    )
    selector.update(changes)
    return dict(kind="rule_effect", effect_ref="effect.dart", targets=selector)


def rule_data(*, actions=None, definitions=None, **changes):
    d = extended_data(**changes)
    # This fixture's old closed doors lack explicit occlusion geometry. Open
    # them so subsequent light-dependent requests have proven mechanical input.
    for scene in d["scenes"]:
        for obj in scene["objects"]:
            if obj["state"] == "closed":
                obj["state"] = "open"
    config = d["world_objects"][-1]["capabilities"][0]
    config.update(schema_version="mechanism/3", rule_effects=definitions or [effect()])
    config["operations"] = [
        dict(operation_id="up", actions=actions or [rule_action()], consume_on_noop=False)
    ]
    # Original lifecycle's failure branch is not used here.
    return d


class FixturePort:
    """Recorded two-HP outcome; no damage/save algorithm or live RE integration."""

    def __init__(self, *, status="accepted", fail_at=None, mutate=None):
        self.requests = []
        self.status, self.fail_at, self.mutate = status, fail_at, mutate

    def evaluate(self, request):
        self.requests.append(deepcopy(request))
        status = self.status if self.fail_at in (None, len(self.requests)) else "accepted"
        common = result_envelope(request)
        if status == "accepted":
            actors = {a.entity_id: a for a in request.state_snapshot.character_states}
            rng = request.rng_context.state.restore()
            rng.getrandbits(32)  # fixed fixture RNG consumption, independent of outcome
            result = MechanismEffectResult(
                **common,
                status=status,
                state_delta=tuple(
                    HPDelta(
                        kind="actor.hp_delta",
                        target_id=target,
                        amount=-2,
                        expected_hp=actors[target].hp_current,
                        resulting_hp=actors[target].hp_current - 2,
                    )
                    for target in request.target_ids
                ),
                proposed_events=tuple(
                    MechanismAudit(kind="damage", target_id=t, succeeded=None, roll=None, amount=2)
                    for t in request.target_ids
                ),
                rng_transition=RNGTransition.between(request.rng_context, rng),
                error=None,
                choice=None,
            )
        else:
            result = MechanismEffectResult(
                **common,
                status=status,
                state_delta=None,
                proposed_events=(),
                rng_transition=None,
                error=None
                if status == "needs_choice"
                else RuleError(code="fixture.refusal", reason="Fixture refusal"),
                choice="choice.fixture" if status == "needs_choice" else None,
            )
        return self.mutate(request, result) if self.mutate else result


def install(sm, port=None):
    port = port or FixturePort()
    sm._objects.mechanisms.effects.port = port
    return port


def state_rows(sm):
    return {
        table: [tuple(r) for r in sm._conn.execute("SELECT * FROM " + table)]
        for table in (
            "characters",
            "mechanical_actors",
            "rng_states",
            "objects",
            "object_lights",
            "object_mechanisms",
            "mechanism_activations",
            "events",
            "event_outbox",
        )
    }


def op():
    return ActivateMechanism(A, LEVER, "0,0", "up")


def test_production_refusal_is_explicit_and_never_selects_fixture(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, rule_data())
    with sm:
        before = state_rows(sm)
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert type(sm._objects.mechanisms.effects.port) is UnsupportedMechanismEffects
        query = sm.query_world_action(sid, actor, op(), p, context=context)
        assert query.status == "unavailable" and query.reason == "unsupported_mechanism_effect"
        receipt = submit(sm, sid, p, context, op(), "production")
        assert receipt.status == "rejected" and receipt.error_code == "unsupported_mechanism_effect"
        assert state_rows(sm) == before


def test_single_source_binding_parameters_and_complete_request(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, rule_data())
    with sm:
        port = install(sm)
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        hp = sm.read_session(sid, p, context=context).character.current_hp
        before_rng = sm.read_rng_context(sid, p, context=context)
        cmd = command(sm, sid, p, context, op(), "source")
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed", receipt
        (request,) = port.requests
        assert request.command_id == "source" and request.session_id == sid
        assert request.source.kind == "mechanism" and request.source.object_id == LEVER
        assert request.source.definition_ref == "definition.lever"
        assert request.source.scene_id == A and request.source.cell == "1,0"
        assert request.target_ids == (actor,)
        assert not hasattr(request, "actor_id")
        assert request.ruleset_binding.model_dump() == asdict(p.ruleset_binding)
        assert request.ruleset_binding.ruleset_id == p.ruleset_binding.ruleset_id
        assert request.rng_context == before_rng
        assert request.effect.save.dc == 12 and request.effect.damage.dice == "1d4"
        assert request.state_snapshot.inventory_state is not None
        assert any(
            a.actor_id == actor and a.cell == "0,0" for a in request.world_dependencies.actor_poses
        )
        assert any(o.object_id == LEVER for o in request.world_dependencies.object_contributions)
        assert sm.read_session(sid, p, context=context).character.current_hp == hp - 2
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert len(port.requests) == 1
        unsupported = UnsupportedMechanismEffects().evaluate(request)
        assert validate_mechanism_result(request, unsupported)[0].status == "unsupported"


@pytest.mark.parametrize(
    "actor_kind,exclude,expected_count",
    [("all", False, 2), ("npc", False, 1), ("pc", False, 1), ("all", True, 1)],
)
def test_area_targets_and_filters(tmp_path, context, actor_kind, exclude, expected_count):
    d = rule_data(
        actions=[
            rule_action(
                scope="area",
                cells=["0,0", "2,2"],
                actor_kind=actor_kind,
                exclude_trigger_actor=exclude,
            )
        ]
    )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        port = install(sm)
        receipt = submit(sm, sid, p, context, op(), "area")
        assert receipt.status == "committed", receipt
        assert len(port.requests[0].target_ids) == expected_count
        assert tuple(sorted(port.requests[0].target_ids)) == port.requests[0].target_ids


def test_ordered_candidate_world_hp_rng_identity_and_reopen(tmp_path, context):
    light = dict(kind="set_light_enabled", target_object_id=CRED, value=True)
    sm, sid, p, db = setup(
        tmp_path, context, rule_data(actions=[rule_action(), light, rule_action()], max_uses=3)
    )
    with sm:
        port = install(sm)
        cmd = command(sm, sid, p, context, op(), "ordered.rules")
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed", receipt
        first, second = port.requests
        assert first.command_id == second.command_id == cmd.command_id
        assert first.source.activation_id == second.source.activation_id
        assert first.source.consequence_id.endswith(".0") and second.source.consequence_id.endswith(
            ".2"
        )
        a = first.target_ids[0]
        hp1 = next(s.hp_current for s in first.state_snapshot.character_states if s.entity_id == a)
        hp2 = next(s.hp_current for s in second.state_snapshot.character_states if s.entity_id == a)
        assert hp2 == hp1 - 2
        assert second.rng_context.version == first.rng_context.version + 1
        assert second.rng_context.state != first.rng_context.state
        assert second.state_snapshot.scene_state.grid != first.state_snapshot.scene_state.grid
        assert sm._world_facts.mechanism_state(sid, LEVER).remaining_uses == 2
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert len(port.requests) == 2
        assert (
            sm.submit_command(replace(cmd, command_id="stale"), p, context=context).status
            == "conflict"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(
                replace(cmd, payload=replace(op(), operation_id="missing")), p, context=context
            )
        final = state_rows(sm)
        events = [sm._read_event(sid, n) for n in receipt.event_seqs]
        activation = next(e for e in events if e.event_type == "mechanism.activated")
        assert len(activation.payload["rule_results"]) == 2
        assert all(e.event_type != "mechanism.rule_effect" for e in events)
    with StateMachine(db, dnd_loader=__import__("lock_support").lock_loader()) as reopened:
        assert reopened.submit_command(cmd, p, context=context) == receipt
        assert state_rows(reopened) == final


@pytest.mark.parametrize(
    "status,receipt_status,code",
    [
        ("unsupported", "rejected", "unsupported_mechanism_effect"),
        ("rejected", "rejected", "mechanism_rule_rejected"),
        ("needs_choice", "pending_choice", "mechanism_needs_choice"),
    ],
)
def test_later_refusal_rolls_back_whole_command_and_replays(
    tmp_path, context, status, receipt_status, code
):
    light = dict(kind="set_light_enabled", target_object_id=CRED, value=True)
    sm, sid, p, _ = setup(
        tmp_path, context, rule_data(actions=[rule_action(), light, rule_action()], max_uses=2)
    )
    with sm:
        port = install(sm, FixturePort(status=status, fail_at=2))
        before = state_rows(sm)
        cmd = command(sm, sid, p, context, op(), "late.refusal")
        receipt = sm.submit_command(cmd, p, context=context)
        assert (receipt.status, receipt.error_code) == (receipt_status, code)
        assert not receipt.event_seqs and state_rows(sm) == before
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert len(port.requests) == 2


@pytest.mark.parametrize(
    "field",
    [
        "session_id",
        "command_id",
        "source",
        "request_digest",
        "input_world_version",
        "input_ruleset_binding",
        "read_set",
        "target_ids",
        "state_delta",
        "rng_transition",
        "proposed_events",
        "schema_version",
    ],
)
def test_malformed_result_fault_has_no_receipt_or_partial_state(tmp_path, context, field):
    def mutate(req, result):
        bad = {
            "session_id": "other.session",
            "command_id": "other.command",
            "source": req.source.model_copy(update={"object_id": CRED}),
            "request_digest": "sha256:wrong",
            "input_world_version": req.state_snapshot.world_version + 1,
            "input_ruleset_binding": req.ruleset_binding.model_copy(
                update={"data_revision": "wrong"}
            ),
            "read_set": (ReadVersion(kind="world", ref="wrong", version=0),),
            "target_ids": ("npc.invalid",),
            "state_delta": (result.state_delta[0].model_copy(update={"target_id": "npc.invalid"}),),
            "rng_transition": result.rng_transition.model_copy(update={"input_version": 999}),
            "proposed_events": (
                result.proposed_events[0].model_copy(update={"target_id": "npc.invalid"}),
            ),
            "schema_version": "unknown/1",
        }
        return result.model_copy(update={field: bad[field]})

    sm, sid, p, _ = setup(
        tmp_path,
        context,
        rule_data(
            actions=[
                dict(kind="set_light_enabled", target_object_id=CRED, value=True),
                rule_action(),
            ]
        ),
    )
    with sm:
        install(sm, FixturePort(mutate=mutate))
        before = state_rows(sm)
        cmd = command(sm, sid, p, context, op(), "bad")
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(cmd, p, context=context)
        assert exc.value.code == "invalid_rule_result"
        assert state_rows(sm) == before
        assert sm.get_command_receipt(sid, "bad", p, context=context) is None


def test_automatic_movement_uses_original_command_and_rolls_back_source(tmp_path, context):
    d = rule_data(triggers=[trigger("enter_region", cells=["1,0"])])
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        port = install(sm, FixturePort(status="unsupported"))
        before = state_rows(sm)
        receipt = submit(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "step.effect")
        assert receipt.status == "rejected", receipt
        assert state_rows(sm) == before
        assert port.requests[0].command_id == "step.effect"
        assert port.requests[0].world_dependencies.actor_poses[0].cell in ("1,0", "2,2")
        assert (
            sm._characters.position(sid, sm.read_session(sid, p, context=context).pc_actor_id).cell
            == "0,0"
        )


@pytest.mark.parametrize(
    "change",
    [
        "unknown_ref",
        "unknown_parameter",
        "bool_dc",
        "bad_cell",
        "outside_cell",
        "duplicate_cell",
        "empty_area",
        "bad_duration",
        "unknown_kind",
        "unknown_version",
    ],
)
def test_invalid_definitions_rejected_at_load(change):
    d = rule_data()
    c = d["world_objects"][-1]["capabilities"][0]
    e, a = c["rule_effects"][0], c["operations"][0]["actions"][0]
    if change == "unknown_ref":
        a["effect_ref"] = "effect.missing"
    elif change == "unknown_parameter":
        e["caster_id"] = "npc.fake"
    elif change == "bool_dc":
        e["save"]["dc"] = True
    elif change in ("bad_cell", "outside_cell", "duplicate_cell", "empty_area"):
        a["targets"].update(
            scope="area",
            cells={
                "bad_cell": ["01,0"],
                "outside_cell": ["999,0"],
                "duplicate_cell": ["0,0", "0,0"],
                "empty_area": [],
            }[change],
        )
    elif change == "bad_duration":
        e["status"] = dict(
            condition="poisoned", duration_seconds=None, repeat_save=None, end_condition="duration"
        )
    elif change == "unknown_kind":
        a["kind"] = "script"
    elif change == "unknown_version":
        c["schema_version"] = "mechanism/99"
    with pytest.raises(StateMachineError):
        _validate(d)


def test_status_request_preserves_source_and_end_condition_but_refuses_unowned_clock(
    tmp_path, context
):
    status = dict(
        condition="poisoned",
        duration_seconds=60,
        repeat_save=dict(ability="con", dc=12, on_success="none"),
        end_condition="duration",
    )
    sm, sid, p, _ = setup(tmp_path, context, rule_data(definitions=[effect(status=status)]))
    with sm:
        port = install(sm)
        before = state_rows(sm)
        receipt = submit(sm, sid, p, context, op(), "status")
        assert receipt.error_code == "unsupported_mechanism_effect_lifecycle"
        assert state_rows(sm) == before
        assert port.requests[0].effect.status.duration_seconds == 60
        assert port.requests[0].effect.status.end_condition == "duration"
        assert port.requests[0].effect.status.repeat_save.ability == "con"


def test_empty_filtered_target_refuses_without_evaluation(tmp_path, context):
    sm, sid, p, _ = setup(
        tmp_path, context, rule_data(actions=[rule_action(scope="area", cells=["1,1"])])
    )
    with sm:
        port = install(sm)
        before = state_rows(sm)
        receipt = submit(sm, sid, p, context, op(), "empty")
        assert receipt.error_code == "invalid_mechanism_targets" and not port.requests
        assert state_rows(sm) == before


@pytest.mark.parametrize("failure", [False, True])
def test_real_re_check_failure_consequence_chains_rng_or_rolls_back(tmp_path, context, failure):
    m = method("disarm")
    m.update(ability="dex", skill="sleight_of_hand", dc=100, trigger_on_failure="up")
    sm, sid, p, _ = setup(tmp_path, context, rule_data(methods=[m]))
    with sm:
        port = install(sm, FixturePort(status="unsupported" if failure else "accepted"))
        before = state_rows(sm)
        rng = sm.read_rng_context(sid, p, context=context)
        operation = CheckMechanism(A, LEVER, "0,0", "disarm")
        cmd = command(sm, sid, p, context, operation, "real.check")
        receipt = sm.submit_command(cmd, p, context=context)
        assert port.requests[0].command_id == cmd.command_id
        assert port.requests[0].rng_context.version == rng.version + 1
        assert port.requests[0].rng_context.state != rng.state
        if failure:
            assert receipt.error_code == "unsupported_mechanism_effect"
            assert state_rows(sm) == before
        else:
            assert receipt.status == "committed", receipt
            final_rng = sm.read_rng_context(sid, p, context=context)
            assert final_rng.version == rng.version + 2
            assert final_rng.state != port.requests[0].rng_context.state
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert len(port.requests) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "expected",
        "arithmetic",
        "healing",
        "extra_field",
        "unknown_delta",
        "rng_state",
        "status_payload",
    ],
)
def test_delta_rng_status_invariants_are_enforced(tmp_path, context, fault):
    def mutate(req, result):
        delta = result.state_delta[0]
        if fault == "duplicate":
            return result.model_copy(update={"state_delta": (delta, delta)})
        if fault == "expected":
            return result.model_copy(
                update={
                    "state_delta": (
                        delta.model_copy(
                            update={
                                "expected_hp": delta.expected_hp + 1,
                                "resulting_hp": delta.resulting_hp + 1,
                            }
                        ),
                    )
                }
            )
        if fault == "arithmetic":
            return result.model_copy(
                update={"state_delta": (delta.model_copy(update={"amount": -3}),)}
            )
        if fault == "healing":
            return result.model_copy(
                update={
                    "state_delta": (
                        delta.model_copy(
                            update={"amount": 1, "resulting_hp": delta.expected_hp + 1}
                        ),
                    )
                }
            )
        if fault in ("extra_field", "unknown_delta"):
            payload = delta.model_dump()
            payload["sql" if fault == "extra_field" else "kind"] = "arbitrary"
            return result.model_copy(update={"state_delta": (payload,)})
        if fault == "rng_state":
            return result.model_copy(
                update={
                    "rng_transition": result.rng_transition.model_copy(
                        update={"state_changed": False}
                    )
                }
            )
        return result.model_copy(
            update={"status": "rejected", "error": RuleError(code="no", reason="no")}
        )

    sm, sid, p, _ = setup(tmp_path, context, rule_data())
    with sm:
        install(sm, FixturePort(mutate=mutate))
        before = state_rows(sm)
        with pytest.raises(StateMachineError) as exc:
            submit(sm, sid, p, context, op(), "invariant")
        assert exc.value.code == "invalid_rule_result"
        assert state_rows(sm) == before


def test_effect_port_schema_json_roundtrip_and_pinned_re_gap(tmp_path, context):
    from typing import get_args

    from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest

    from trpg_state_machine.mechanism_effects import MechanismEffectRequest

    assert "rules.effect" not in get_args(
        RuleEvaluationRequest.model_fields["operation_kind"].annotation
    )
    assert RuleEvaluationRequest.model_fields["actor_id"].is_required()
    sm, sid, p, _ = setup(tmp_path, context, rule_data())
    with sm:
        port = install(sm)
        assert submit(sm, sid, p, context, op(), "roundtrip").status == "committed"
        request = port.requests[0]
        assert MechanismEffectRequest.model_validate_json(request.model_dump_json()) == request
        assert MechanismEffectRequest.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize("fault", ["evaluator", "owner"])
def test_evaluator_and_owner_storage_fault_do_not_cache_refusal(
    tmp_path, context, monkeypatch, fault
):
    sm, sid, p, _ = setup(tmp_path, context, rule_data())
    with sm:
        port = install(sm)
        before = state_rows(sm)
        original = sm._characters.set_hp

        def fail(*args):
            if fault == "owner":
                original(*args)
                raise StateMachineError("storage_error", "Fixture storage fault after owner write")
            raise RuntimeError("fixture evaluator failure")

        monkeypatch.setattr(
            port if fault == "evaluator" else sm._characters,
            "evaluate" if fault == "evaluator" else "set_hp",
            fail,
        )
        with pytest.raises(StateMachineError) as exc:
            submit(sm, sid, p, context, op(), "retry")
        assert exc.value.code == (
            "rule_evaluation_failed" if fault == "evaluator" else "storage_error"
        )
        assert state_rows(sm) == before
        assert sm.get_command_receipt(sid, "retry", p, context=context) is None
        install(sm)
        monkeypatch.setattr(sm._characters, "set_hp", original)
        assert submit(sm, sid, p, context, op(), "retry").status == "committed"


def test_conflicting_candidate_is_not_adopted(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, rule_data())
    with sm:

        def mutate(req, result):
            sm._sessions.set_world_version(sid, req.state_snapshot.world_version + 1)
            return result

        install(sm, FixturePort(mutate=mutate))
        before = state_rows(sm)
        receipt = submit(sm, sid, p, context, op(), "conflict")
        assert receipt.status == "conflict" and receipt.error_code == "version_conflict"
        assert state_rows(sm) == before
        assert sm.read_session(sid, p, context=context).world_version == 0


def test_attack_definition_and_accepted_miss_empty_delta(tmp_path, context):
    def miss(req, result):
        return result.model_copy(
            update={
                "state_delta": (),
                "proposed_events": (
                    MechanismAudit(
                        kind="attack",
                        target_id=req.target_ids[0],
                        succeeded=False,
                        roll=2,
                        amount=None,
                    ),
                ),
            }
        )

    d = rule_data(
        definitions=[effect(save=None, attack=dict(bonus=5, mode="ranged", range_feet=30))]
    )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        port = install(sm, FixturePort(mutate=miss))
        before = sm.read_session(sid, p, context=context)
        receipt = submit(sm, sid, p, context, op(), "miss")
        after = sm.read_session(sid, p, context=context)
        assert receipt.status == "committed", receipt
        assert after.character.current_hp == before.character.current_hp
        assert after.rng.rng_version == before.rng.rng_version + 1
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
        assert port.requests[0].effect.attack.bonus == 5


def test_zero_hp_packet_is_explicitly_unsupported_without_partial_damage(tmp_path, context):
    def zero(req, result):
        delta = result.state_delta[0]
        return result.model_copy(
            update={
                "state_delta": (
                    delta.model_copy(update={"amount": -delta.expected_hp, "resulting_hp": 0}),
                )
            }
        )

    sm, sid, p, _ = setup(tmp_path, context, rule_data())
    with sm:
        install(sm, FixturePort(mutate=zero))
        before = state_rows(sm)
        receipt = submit(sm, sid, p, context, op(), "zero")
        assert receipt.error_code == "unsupported_mechanism_effect_delta"
        assert state_rows(sm) == before
