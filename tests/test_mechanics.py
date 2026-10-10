import json
import sqlite3
from dataclasses import asdict, replace

import pytest
import yaml

from trpg_state_machine import SetSceneFlag, StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.mechanics import (
    HPChange,
    ResourceSpend,
    RuleOperation,
    StateDelta,
    SyntheticEvaluator,
)
from trpg_state_machine.package import (
    SYNTHETIC_EVALUATION_RULESET,
    SYNTHETIC_RULESET,
    load_adventure_package,
)


@pytest.fixture
def mechanical_package(tmp_path, package_data):
    package_data["manifest"]["ruleset_binding"] = asdict(SYNTHETIC_EVALUATION_RULESET)
    for pc in package_data["pc_templates"]:
        pc["character"]["resources"] = {"resource.tokens": 3}
        pc["character"]["inventory"] = ["item.synthetic_token"]
    path = tmp_path / "mechanics.yaml"
    path.write_text(yaml.safe_dump(package_data), "utf-8")
    result = load_adventure_package(path)
    assert result.status == "valid", result.errors
    return result.package


def setup_mechanical(sm, package, request_for, context, difficulty=1, kind="synthetic_attempt"):
    request = replace(
        request_for(pc="pc_template.visitor"),
        adventure_id=package.adventure_id,
        package_version=package.package_version,
        package_digest=package.package_digest,
    )
    created = sm.create_session(request, package, context=context)
    snapshot = sm.read_session(created.session_id, package, context=context)
    payload = RuleOperation(
        kind,
        snapshot.pc_actor_id,
        "resource.tokens",
        "scene.platform",
        "flag.lamp_checked",
        difficulty,
    )
    return TypedCommand(
        created.session_id, "mechanical.1", snapshot.pc_actor_id, "rule_operation", payload, 0
    )


class InspectEvaluator(SyntheticEvaluator):
    def __init__(self, transform=lambda request, result: result):
        self.transform = transform
        self.calls = 0
        self.requests = []
        self.results = []

    def evaluate(self, request):
        self.calls += 1
        self.requests.append(request)
        result = super().evaluate(request)
        self.results.append(result)
        return self.transform(request, result)


@pytest.mark.parametrize("difficulty,success,hp", [(1, True, 12), (7, False, 11)])
def test_cross_domain_atomic_commit_and_restart(
    tmp_path, mechanical_package, context, request_for, difficulty, success, hp
):
    package = mechanical_package
    database = tmp_path / "state.db"
    evaluator = InspectEvaluator()
    with StateMachine(database, evaluator=evaluator) as sm:
        command = setup_mechanical(sm, package, request_for, context, difficulty)
        before = sm.read_session(command.session_id, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed" and receipt.world_version == 1
        assert sm.submit_command(command, package, context=context) == receipt
        assert evaluator.calls == 1
        request = evaluator.requests[0]
        assert request.snapshot.characters[0].inventory == ("item.synthetic_token",)
        assert request.snapshot.characters[0].effects == ()
        assert request.rng == before.rng
    with StateMachine(database, evaluator=evaluator) as sm:
        assert sm.submit_command(command, package, context=context) == receipt
        snapshot = sm.resume_session(command.session_id, package, context=context)
        assert snapshot.character.current_hp == hp
        assert snapshot.character.resources == {"resource.tokens": 2}
        assert snapshot.scenes["scene.platform"].local_flags["flag.lamp_checked"] is success
        assert snapshot.rng.position == snapshot.rng.rng_version == 1
        assert json.loads(snapshot.rng.state_json) == {"counter": 1}
        assert evaluator.calls == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT current_hp,resources_json FROM characters").fetchone() == (
            hp,
            '{"resource.tokens":2}',
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "kind,difficulty,target,status,code",
    [
        ("unknown", 1, None, "unsupported", "unsupported_rule_operation"),
        ("synthetic_attempt", None, None, "needs_choice", "pending_choice_unsupported"),
        ("synthetic_attempt", 1, "npc.caretaker", "rejected", "mechanical_precondition"),
    ],
)
def test_nonaccepted_four_state_protocol(
    tmp_path, mechanical_package, context, request_for, kind, difficulty, target, status, code
):
    evaluator = InspectEvaluator()
    with StateMachine(tmp_path / "state.db", evaluator=evaluator) as sm:
        command = setup_mechanical(sm, mechanical_package, request_for, context, difficulty, kind)
        if target:
            command = replace(command, payload=replace(command.payload, target_actor_id=target))
        before = sm.read_session(command.session_id, mechanical_package, context=context)
        receipt = sm.submit_command(command, mechanical_package, context=context)
        assert receipt.status == "rejected" and receipt.error_code == code
        result = evaluator.results[0]
        assert result.status == status and result.delta is None
        assert result.proposed_events == () and result.rng_transition is None
        assert sm.read_session(command.session_id, mechanical_package, context=context) == before
        assert sm.submit_command(command, mechanical_package, context=context) == receipt
        assert evaluator.calls == 1


@pytest.mark.parametrize(
    "transform,code",
    [
        (lambda q, r: replace(r, request_id="wrong"), "invalid_rule_result"),
        (lambda q, r: replace(r, input_world_version=99), "invalid_rule_result"),
        (lambda q, r: replace(r, read_set_digest="wrong"), "invalid_rule_result"),
        (lambda q, r: replace(r, status="rejected"), "invalid_rule_result"),
        (lambda q, r: replace(r, delta=StateDelta(({"patch": True},))), "unsupported_delta"),
        (
            lambda q, r: replace(r, delta=StateDelta((HPChange("npc.caretaker", 11, 12),))),
            "invalid_delta",
        ),
        (
            lambda q, r: replace(r, delta=StateDelta((HPChange(q.actor_id, 11, 999),))),
            "invalid_delta",
        ),
        (
            lambda q, r: replace(
                r, delta=StateDelta((ResourceSpend(q.actor_id, "resource.tokens", 3, -1),))
            ),
            "invalid_delta",
        ),
        (
            lambda q, r: replace(
                r,
                delta=StateDelta((SetSceneFlag("scene.workshop", "flag.workshop_checked", True),)),
            ),
            "invalid_delta",
        ),
        (lambda q, r: replace(r, delta=StateDelta(r.delta.operations * 2)), "invalid_delta"),
        (
            lambda q, r: replace(
                r,
                rng_transition=replace(
                    r.rng_transition, after=replace(r.rng_transition.after, state_id="foreign")
                ),
            ),
            "invalid_rule_result",
        ),
    ],
)
def test_untrusted_result_delta_rng_refused(
    tmp_path, mechanical_package, context, request_for, transform, code
):
    with StateMachine(tmp_path / "state.db", evaluator=InspectEvaluator(transform)) as sm:
        command = setup_mechanical(sm, mechanical_package, request_for, context)
        before = sm.read_session(command.session_id, mechanical_package, context=context)
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, mechanical_package, context=context)
        assert error.value.code == code
        assert sm.read_session(command.session_id, mechanical_package, context=context) == before
        assert (
            sm.get_command_receipt(
                command.session_id, command.command_id, mechanical_package, context=context
            )
            is None
        )


def test_evaluation_outside_transaction_and_version_race(
    tmp_path, mechanical_package, context, request_for
):
    database = tmp_path / "state.db"

    def race(request, result):
        assert not sm._conn.in_transaction
        with StateMachine(database) as other:
            command = TypedCommand(
                request.session_id,
                "racing",
                request.actor_id,
                "set_scene_flag",
                SetSceneFlag("scene.platform", "flag.lamp_checked", True),
                0,
            )
            other.submit_command(command, mechanical_package, context=context)
        return result

    with StateMachine(database, evaluator=InspectEvaluator(race)) as sm:
        command = setup_mechanical(sm, mechanical_package, request_for, context)
        receipt = sm.submit_command(command, mechanical_package, context=context)
        assert receipt.status == "conflict"
        snapshot = sm.read_session(command.session_id, mechanical_package, context=context)
        assert snapshot.character.resources["resource.tokens"] == 3
        assert snapshot.rng.position == 0 and snapshot.world_version == 1


@pytest.mark.parametrize("table", ["command_receipts", "event_outbox", "rng_states"])
def test_cross_domain_failure_rolls_back(tmp_path, mechanical_package, context, request_for, table):
    evaluator = InspectEvaluator()
    with StateMachine(tmp_path / "state.db", evaluator=evaluator) as sm:
        command = setup_mechanical(sm, mechanical_package, request_for, context)
        before = sm.read_session(command.session_id, mechanical_package, context=context)
        action = "UPDATE" if table == "rng_states" else "INSERT"
        sm._conn.execute(
            f"CREATE TRIGGER fail_commit BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, mechanical_package, context=context)
        assert error.value.code == "storage_error"
        assert sm.read_session(command.session_id, mechanical_package, context=context) == before
        sm._conn.execute("DROP TRIGGER fail_commit")
        assert sm.submit_command(command, mechanical_package, context=context).status == "committed"
        assert evaluator.results[0] == evaluator.results[1]


def test_mutating_evaluator_cannot_change_authority_input(
    tmp_path, mechanical_package, context, request_for
):
    def corrupt(request, result):
        request.snapshot.characters[0].resources["resource.tokens"] = 100
        return replace(
            result, delta=StateDelta((ResourceSpend(request.actor_id, "resource.tokens", 100, 99),))
        )

    with StateMachine(tmp_path / "state.db", evaluator=InspectEvaluator(corrupt)) as sm:
        command = setup_mechanical(sm, mechanical_package, request_for, context)
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, mechanical_package, context=context)
        assert error.value.code == "invalid_delta"


def test_combat_domain_explicitly_unsupported(tmp_path, mechanical_package, context, request_for):
    with StateMachine(tmp_path / "state.db") as sm:
        command = setup_mechanical(sm, mechanical_package, request_for, context)
        command = replace(command, payload=replace(command.payload, domain="combat"))
        receipt = sm.submit_command(command, mechanical_package, context=context)
        assert receipt.status == "rejected" and receipt.error_code == "unsupported_rule_operation"


def test_evaluator_exception_retry_uses_same_rng(
    tmp_path, mechanical_package, context, request_for
):
    seen = []

    def unavailable(request, result):
        seen.append(request.rng)
        if len(seen) == 1:
            raise TimeoutError("evaluation response lost")
        return result

    with StateMachine(tmp_path / "state.db", evaluator=InspectEvaluator(unavailable)) as sm:
        command = setup_mechanical(sm, mechanical_package, request_for, context)
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, mechanical_package, context=context)
        assert error.value.code == "rule_evaluation_failed"
        assert sm.submit_command(command, mechanical_package, context=context).status == "committed"
        assert seen[0] == seen[1]


@pytest.mark.parametrize("binding", [SYNTHETIC_RULESET, None])
def test_evaluator_configuration_error_is_not_a_receipt(
    tmp_path, mechanical_package, context, request_for, binding
):
    database = tmp_path / "state.db"
    evaluator = InspectEvaluator()
    evaluator.ruleset_binding = binding
    with StateMachine(database, evaluator=evaluator) as sm:
        command = setup_mechanical(sm, mechanical_package, request_for, context)
        before = sm.read_session(command.session_id, mechanical_package, context=context)
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, mechanical_package, context=context)
        assert error.value.code == "ruleset_binding_mismatch"
        assert evaluator.calls == 0
        assert sm.read_session(command.session_id, mechanical_package, context=context) == before
        assert sm._conn.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0
        assert sm._conn.execute("SELECT count(*) FROM events").fetchone()[0] == before.event_seq
    evaluator.ruleset_binding = SYNTHETIC_EVALUATION_RULESET
    with StateMachine(database, evaluator=evaluator) as sm:
        receipt = sm.submit_command(command, mechanical_package, context=context)
        assert receipt.status == "committed" and evaluator.calls == 1
        evaluator.ruleset_binding = binding
        # A stored outcome is independent of later deployment configuration.
        assert sm.submit_command(command, mechanical_package, context=context) == receipt
        assert evaluator.calls == 1


def test_result_binding_error_can_retry_original_command(
    tmp_path, mechanical_package, context, request_for
):
    evaluator = InspectEvaluator(
        lambda request, result: replace(result, ruleset_binding=SYNTHETIC_RULESET)
    )
    with StateMachine(tmp_path / "state.db", evaluator=evaluator) as sm:
        command = setup_mechanical(sm, mechanical_package, request_for, context)
        before = sm.read_session(command.session_id, mechanical_package, context=context)
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, mechanical_package, context=context)
        assert error.value.code == "ruleset_binding_mismatch"
        assert sm.read_session(command.session_id, mechanical_package, context=context) == before
        assert sm._conn.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0
        assert sm._conn.execute("SELECT count(*) FROM events").fetchone()[0] == before.event_seq
        evaluator.transform = lambda request, result: result
        assert sm.submit_command(command, mechanical_package, context=context).status == "committed"
        assert evaluator.requests[0].rng == evaluator.requests[1].rng


@pytest.mark.parametrize("fault_table", [None, "command_receipts", "trigger_history"])
def test_mechanical_cascade_rejection_is_atomic_durable_and_idempotent(
    tmp_path, mechanical_package, context, request_for, fault_table
):
    from test_events import event_data, load_event_package

    # Same rule candidate changes HP, pays a resource, draws RNG and changes a flag;
    # its world consequences exceed the bound after partially updating trigger history.
    data = yaml.safe_load((tmp_path / "mechanics.yaml").read_text("utf-8"))
    package = load_event_package(
        tmp_path, data, [event_data(f"event.reward_{i}") for i in range(33)]
    ).package
    evaluator = InspectEvaluator()
    database = tmp_path / "state.db"
    with StateMachine(database, evaluator=evaluator) as sm:
        command = setup_mechanical(sm, package, request_for, context)
        before = sm.read_session(command.session_id, package, context=context)
        if fault_table is None:
            evaluator.ruleset_binding = SYNTHETIC_RULESET
            with pytest.raises(StateMachineError) as configuration:
                sm.submit_command(command, package, context=context)
            assert configuration.value.code == "ruleset_binding_mismatch"
            assert sm._conn.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0
            evaluator.ruleset_binding = SYNTHETIC_EVALUATION_RULESET
        if fault_table:
            sm._conn.execute(
                f"CREATE TRIGGER fault BEFORE INSERT ON {fault_table} "
                "BEGIN SELECT RAISE(ABORT,'injected storage failure'); END"
            )
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(command, package, context=context)
            assert error.value.code == "storage_error"
            assert sm.read_session(command.session_id, package, context=context) == before
            assert sm._conn.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0
            sm._conn.execute("DROP TRIGGER fault")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "rejected" and receipt.error_code == "event_cascade_limit"
        assert receipt.world_version == 0 and receipt.event_seqs == ()
        assert sm.read_session(command.session_id, package, context=context) == before
        assert sm.submit_command(command, package, context=context) == receipt
        calls = evaluator.calls
        assert calls == 1 + int(fault_table is not None)
    with StateMachine(database, evaluator=evaluator) as sm:
        assert (
            sm.get_command_receipt(command.session_id, command.command_id, package, context=context)
            == receipt
        )
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_session(command.session_id, package, context=context) == before
        assert evaluator.calls == calls
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM trigger_history").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == before.event_seq
        assert (
            conn.execute("SELECT count(*) FROM event_outbox").fetchone()[0] == 2 * before.event_seq
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
