"""Session ownership through real public creation, execution and recovery paths."""

import json
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import NPC, WEAPON, create, initialize, loader, make_package
from spatial_support import A, B, command
from test_mechanics import InspectEvaluator, setup_mechanical
from test_migrations import stored_rows
from test_real_checks import check_command, check_setup

from trpg_state_machine import MoveActor, StateMachine, StateMachineError
from trpg_state_machine.package import SYNTHETIC_EVALUATION_RULESET, _validate


@pytest.fixture
def synthetic(package_data):
    package_data["manifest"]["ruleset_binding"] = asdict(SYNTHETIC_EVALUATION_RULESET)
    for pc in package_data["pc_templates"]:
        pc["character"]["resources"] = {"resource.tokens": 3}
        pc["character"]["inventory"] = ["item.synthetic_token"]
    return _validate(package_data)


def poison_package(package):
    return replace(
        package,
        ruleset_binding=replace(package.ruleset_binding, data_revision="sha256:" + "f" * 64),
    )


def isolate_pin_guard(sm, monkeypatch):
    """Bypass ONLY package equality to distinguish two otherwise equal sources.

    This is deliberately impossible via the public API. Real pins are tested
    separately; evaluator/request/query code must still use durable Session data.
    """
    calls = []
    monkeypatch.setattr(
        sm._sessions, "_check_package_pin", lambda row, p: calls.append(row["session_id"])
    )
    return calls


@pytest.mark.parametrize("real", [False, True])
def test_creation_rejects_actual_evaluator_mismatch_without_partial_session(
    tmp_path, package_data, context, synthetic, request_for, real
):
    assets = loader()
    package = make_package(package_data, assets) if real else synthetic
    evaluator = InspectEvaluator()
    evaluator.ruleset_binding = replace(SYNTHETIC_EVALUATION_RULESET, evaluator_version="wrong")
    with StateMachine(tmp_path / "wrong.db", dnd_loader=loader(), evaluator=evaluator) as sm:
        if real:
            # Same interface, different effective corpus; never patch verify_ruleset.
            sm._rules.loader = type(assets)()
        before = stored_rows(tmp_path / "wrong.db")
        with pytest.raises(StateMachineError) as exc:
            create(sm, package, context) if real else sm.create_session(
                replace(request_for(), package_digest=package.package_digest),
                package,
                context=context,
            )
        assert exc.value.code == "ruleset_binding_mismatch"
        assert stored_rows(tmp_path / "wrong.db") == before


def test_creation_delegate_and_all_fixed_fields_survive_restart(
    tmp_path, package_data, context, monkeypatch
):
    assets = loader()
    package = make_package(package_data, assets)
    db = tmp_path / "pins.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        calls, original = [], sm._creation.create

        def tracked(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        monkeypatch.setattr(sm._creation, "create", tracked)
        sid = create(sm, package, context)
        assert create(sm, package, context) == sid
        assert len(calls) == 2
        saved = sm.read_session(sid, package, context=context)
        assert saved.ruleset_binding == package.ruleset_binding
        assert saved.package_digest == package.package_digest
        assert saved.world_version == saved.rng.rng_version == 0
        assert saved.player.player_id == context.player_id
    with StateMachine(db, dnd_loader=assets) as sm:
        assert sm.resume_session(sid, package, context=context) == saved


def test_synthetic_request_and_fingerprint_use_session_even_with_isolated_package_guard(
    tmp_path, synthetic, context, request_for, monkeypatch
):
    evaluator = InspectEvaluator()
    with StateMachine(tmp_path / "source.db", evaluator=evaluator) as sm:
        cmd = setup_mechanical(sm, synthetic, request_for, context)
        pins = isolate_pin_guard(sm, monkeypatch)
        receipt = sm.submit_command(cmd, poison_package(synthetic), context=context)
        assert receipt.status == "committed"
        assert evaluator.requests[0].ruleset_binding == synthetic.ruleset_binding
        assert pins
        assert all(
            json.loads(row[0])["ruleset_binding"] == asdict(synthetic.ruleset_binding)
            for row in sm._conn.execute("SELECT evidence_json FROM event_evidence")
        )
        # Durable fingerprints retain the old canonical encoding and replay.
        assert sm.submit_command(cmd, synthetic, context=context) == receipt
        assert evaluator.calls == 1


def test_real_checks_reads_and_verification_use_durable_binding(
    tmp_path, package_data, context, monkeypatch
):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        wrong = poison_package(package)
        pins = isolate_pin_guard(sm, monkeypatch)
        import trpg_state_machine.rules_adapter as module

        seen, requests, verify = [], [], module.verify_ruleset
        evaluate = sm._rules._evaluate_dnd

        def verified(binding, assets):
            assert asdict(package.ruleset_binding) == binding.model_dump()
            seen.append(binding)
            return verify(binding, assets)

        async def evaluated(request):
            requests.append(request)
            return await evaluate(request)

        monkeypatch.setattr(module, "verify_ruleset", verified)
        monkeypatch.setattr(sm._rules, "_evaluate_dnd", evaluated)
        sm.read_noncombat_snapshot(sid, wrong, context=context)
        sm.read_rng_context(sid, wrong, context=context)
        cmd = check_command(sm, sid, wrong, context)
        assert sm.submit_command(cmd, wrong, context=context).status == "committed"
        assert requests[0].ruleset_binding.model_dump() == asdict(package.ruleset_binding)
        assert len(seen) >= 4 and pins


def test_combat_initialization_and_availability_use_session_and_validate_response(
    tmp_path, package_data, context, monkeypatch
):
    assets = loader()
    package = make_package(package_data, assets)
    with StateMachine(tmp_path / "availability.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        wrong = poison_package(package)
        isolate_pin_guard(sm, monkeypatch)
        import trpg_state_machine.rules_adapter as module

        verify, query, seen = module.verify_ruleset, module.query_action_availability, []

        def verified(binding, assets):
            assert binding.model_dump() == asdict(package.ruleset_binding)
            return verify(binding, assets)

        def queried(request, **kwargs):
            seen.append(request)
            assert request.ruleset_binding.model_dump() == asdict(package.ruleset_binding)
            return query(request, **kwargs)

        monkeypatch.setattr(module, "verify_ruleset", verified)
        assert initialize(sm, sid, wrong, context).status == "committed"
        state = sm.read_session(sid, wrong, context=context)
        sm.read_combat_snapshot(sid, wrong, context=context)
        monkeypatch.setattr(module, "query_action_availability", queried)
        payload = CombatIntentPayload(intent_type="attack", target_id=NPC, weapon_id=WEAPON)
        sm.query_action_availability(sid, state.pc_actor_id, payload, wrong, context=context)
        assert seen

        def invalid(request, **kwargs):
            return query(request, **kwargs).model_copy(
                update={
                    "ruleset_binding": request.ruleset_binding.model_copy(
                        update={"evaluator_version": "wrong"}
                    )
                }
            )

        monkeypatch.setattr(module, "query_action_availability", invalid)
        before = stored_rows(tmp_path / "availability.db")
        with pytest.raises(StateMachineError) as exc:
            sm.query_action_availability(sid, state.pc_actor_id, payload, package, context=context)
        assert exc.value.code == "invalid_rule_result"
        assert stored_rows(tmp_path / "availability.db") == before


def test_real_result_binding_mismatch_cannot_commit(tmp_path, package_data, context, monkeypatch):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        evaluate = sm._rules._evaluate_dnd

        async def invalid(request):
            result = await evaluate(request)
            return result.model_copy(
                update={
                    "ruleset_binding": request.ruleset_binding.model_copy(
                        update={"evaluator_version": "wrong"}
                    )
                }
            )

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", invalid)
        before = stored_rows(tmp_path / "checks.db")
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(check_command(sm, sid, package, context), package, context=context)
        assert exc.value.code == "invalid_rule_result"
        assert stored_rows(tmp_path / "checks.db") == before


def test_openable_availability_request_uses_session(tmp_path, context, monkeypatch):
    from lock_support import CRED, DOOR
    from test_openable_checks import setup_check

    from trpg_state_machine import PickLock

    sm, sid, package, _ = setup_check(tmp_path, context)
    with sm:
        isolate_pin_guard(sm, monkeypatch)
        import dnd5e_engine.evaluation_openable as module

        check, requests = module.openable_support_failure, []

        def inspected(spec, **kwargs):
            requests.append(spec.request)
            assert spec.request.ruleset_binding.model_dump() == asdict(package.ruleset_binding)
            return check(spec, **kwargs)

        monkeypatch.setattr(module, "openable_support_failure", inspected)
        state = sm.read_session(sid, package, context=context)
        result = sm.query_world_action(
            sid,
            state.pc_actor_id,
            PickLock(A, DOOR, "lock.outer", CRED, "0,0"),
            poison_package(package),
            context=context,
        )
        assert result.status == "available" and requests


def test_mechanism_contract_port_request_uses_session(tmp_path, context, monkeypatch):
    from test_mechanism_effects import install, op, rule_data
    from world_support import setup, submit

    sm, sid, package, _ = setup(tmp_path, context, rule_data())
    with sm:
        port = install(sm)
        isolate_pin_guard(sm, monkeypatch)
        result = submit(sm, sid, poison_package(package), context, op(), "source.effect")
        assert result.status == "committed"
        assert port.requests[0].ruleset_binding.model_dump() == asdict(package.ruleset_binding)


def test_restore_rejects_missing_capability_state_without_defaults(tmp_path, context):
    from lock_support import lock_loader
    from test_world_durability import durability_data

    package = _validate(durability_data())
    db = tmp_path / "missing-capability.db"
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        sid = create(sm, package, context)
        with sm._transaction(write=True):
            sm._conn.execute("DELETE FROM object_durability WHERE session_id=?", (sid,))
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as exc:
            sm.resume_session(sid, package, context=context)
        assert exc.value.code == "incomplete_session"
        assert stored_rows(db) == before


@pytest.mark.parametrize("table", ["rng_states", "creation_commands", "event_outbox"])
def test_creation_failure_rolls_back_every_participant(
    tmp_path, package, context, request_for, table
):
    db = tmp_path / "fault.db"
    with StateMachine(db) as sm:
        sm._conn.execute(
            f"CREATE TRIGGER fail BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as exc:
            sm.create_session(request_for(), package, context=context)
        assert exc.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fail")
        assert sm.create_session(request_for(), package, context=context).session_id


def test_lazy_npc_operation_authorizes_then_instantiates_without_entry_and_rolls_back(
    tmp_path, package, context
):
    db = tmp_path / "lazy.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        operation = MoveActor("npc.caretaker", B)
        cmd = command(sm, sid, package, context, operation, "lazy", "npc.caretaker")
        initial = stored_rows(db)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(cmd, package, context=replace(context, controlled_npc_ids=()))
        assert exc.value.code == "unauthorized"
        assert stored_rows(db) == initial
        assert (
            sm.query_world_action(sid, cmd.actor_id, operation, package, context=context).status
            == "available"
        )
        assert stored_rows(db) == initial
        # Late failure rolls back newly created scene, objects/NPCs and movement.
        sm._conn.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(cmd, package, context=context)
        assert exc.value.code == "storage_error"
        assert stored_rows(db) == initial
        sm._conn.execute("DROP TRIGGER fail")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed"
        saved = sm.resume_session(sid, package, context=context)
        assert saved.current_scene_id == A and saved.scenes[B].visited_count == 0
        assert saved.actors["npc.caretaker"].current_scene_id == B
        assert all(
            e.event_type != "scene.entered"
            for e in sm.read_events(sid, package, context=context, after_seq=2)
        )
        assert sm.submit_command(cmd, package, context=context) == receipt
        with sm._transaction(write=True):
            assert not sm.scene_lifecycle.ensure_scene(sid, B, package)
        assert sm.read_session(sid, package, context=context) == saved


def test_lazy_creation_rejects_new_package_even_if_scene_does_not_exist(tmp_path, package, context):
    with StateMachine(tmp_path / "pin-lazy.db") as sm:
        sid = create(sm, package, context)
        before = stored_rows(tmp_path / "pin-lazy.db")
        with pytest.raises(StateMachineError) as exc, sm._transaction(write=True):
            sm.scene_lifecycle.ensure_scene(sid, B, replace(package, package_digest="changed"))
        assert exc.value.code == "package_digest_mismatch"
        assert stored_rows(tmp_path / "pin-lazy.db") == before


def test_existing_scene_constructor_still_composes_without_facade(tmp_path, package, context):
    from test_domain_runtime import components, seed

    from trpg_state_machine import SceneLifecycleService

    c = components(tmp_path / "constructor.db")
    try:
        sid, _ = seed(c, package, context)
        scenes = SceneLifecycleService(
            c.store, c.sessions, c.characters, c.facts, c.capture, c.world.npc_bootstrap
        )
        before = stored_rows(tmp_path / "constructor.db")
        with c.store.transaction(write=True):
            assert not scenes.ensure_scene(sid, A, package)
        assert stored_rows(tmp_path / "constructor.db") == before
    finally:
        c.store.close()


@pytest.mark.parametrize("table", ["characters", "world_states", "objects"])
def test_restore_rejects_incomplete_existing_facts_without_template_repair(
    tmp_path, package, context, table
):
    db = tmp_path / "incomplete.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        with sm._transaction(write=True):
            sm._conn.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
        before = stored_rows(db)
    with StateMachine(db) as sm:
        with pytest.raises(StateMachineError) as exc:
            sm.resume_session(sid, package, context=context)
        assert exc.value.code == "incomplete_session"
        assert stored_rows(db) == before
