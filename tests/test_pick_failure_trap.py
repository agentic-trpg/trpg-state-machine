"""Author and resolve exact lock failure gates through real public commands."""

import json
from copy import deepcopy

import pytest
import yaml
from dnd5e_engine import evaluation
from dnd5e_engine.evaluation_rng import RNGState
from lock_support import CRED, DOOR, A
from test_lockable_passages import acquire
from test_mechanism_effects import state_rows
from test_mechanism_save_cover import cover_data
from test_mechanism_save_gate import state_gated_data
from test_mechanism_service import config, trigger
from test_openable_checks import check_data
from test_world_mechanisms import LEVER
from world_support import create, submit

from trpg_state_machine import (
    ForceOpen,
    StateMachine,
    StateMachineError,
    TrustedContext,
    load_adventure_package,
    world_command_from_data,
)


def pick_failure_data(*, check_dc=100, save_dc=100, status="armed"):
    """Build raw Package YAML data for two locks and an authored failure gate."""
    data, loader = check_data(dc=check_dc)
    mechanism = state_gated_data(dc=save_dc)
    data["manifest"]["schema_version"] = "state-machine-foundation/10"
    data["scenes"][0]["objects"].append(
        deepcopy(
            next(obj for obj in mechanism["scenes"][0]["objects"] if obj["object_id"] == LEVER)
        )
    )
    definition = deepcopy(mechanism["world_objects"][-1])
    definition["capabilities"][0].update(triggers=[], initial_status=status)
    data["world_objects"].append(definition)
    light = next(obj for obj in mechanism["world_objects"] if obj["object_id"] == CRED)
    target = next(obj for obj in data["world_objects"] if obj["object_id"] == CRED)
    target["capabilities"] = deepcopy(light["capabilities"])
    locks = next(
        c["locks"] for c in data["world_objects"][0]["capabilities"] if c["kind"] == "locks"
    )
    locks[0]["pick_failure_mechanism_id"] = LEVER
    locks[1].update(pick_policy="scenario", pick_dc=100)
    return data, loader


def write_package(tmp_path, data):
    """Write the portable shape using the same loader reached by a Package producer."""
    path = tmp_path / "pick-failure.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_adventure_package(path)


def test_authored_lock_reference_compiles_to_one_existing_trigger(tmp_path):
    data, _ = pick_failure_data()
    loaded = write_package(tmp_path, data)
    assert loaded.status == "valid", loaded.errors
    mechanism = next(d for d in loaded.package.world_objects if d.object_id == LEVER)
    (trigger,) = mechanism.capability("mechanism").triggers
    assert (trigger.kind, trigger.scene_id, trigger.target_object_id, trigger.lock_id) == (
        "check_result",
        A,
        DOOR,
        "lock.outer",
    )
    assert trigger.operation_id == "up" and trigger.succeeded is False


def setup_pick(tmp_path, context, data, loader):
    """Create through the public YAML loader and equip the fixture's real tool."""
    loaded = write_package(tmp_path, data)
    assert loaded.status == "valid", loaded.errors
    package = loaded.package
    db = tmp_path / "pick.sqlite"
    sm = StateMachine(db, dnd_loader=loader)
    sid = create(sm, package, context)
    acquire(sm, sid, package, context, iid=CRED, key="tools")
    return sm, sid, package, db


def pick_command(sm, sid, package, context, key="pick", *, lock="lock.outer", tool=CRED):
    """Parse the same JSON command surface exposed to callers."""
    state = sm.read_session(sid, package, context=context)
    return world_command_from_data(
        json.loads(
            json.dumps(
                {
                    "schema_version": "sm-command/1",
                    "session_id": sid,
                    "command_id": key,
                    "actor_id": state.pc_actor_id,
                    "command_type": "world.pick_lock",
                    "expected_world_version": state.world_version,
                    "payload": {
                        "scene_id": A,
                        "object_id": DOOR,
                        "lock_id": lock,
                        "tool_instance_id": tool,
                        "expected_cell": "0,0",
                    },
                }
            )
        )
    )


@pytest.mark.parametrize("save_dc", [0, 100])
def test_failed_exact_pick_adopts_ordered_check_and_save(tmp_path, context, monkeypatch, save_dc):
    data, loader = pick_failure_data(save_dc=save_dc)
    sm, sid, package, db = setup_pick(tmp_path, context, data, loader)
    with sm:
        real_save = evaluation.evaluate_bound_save
        calls = []
        cmd = pick_command(sm, sid, package, context)
        before = sm.read_rng_context(sid, package, context=context)
        initial = state_rows(sm)

        def outside_write(request, *, loader):
            assert sm._store._scope is None and not sm._conn.in_transaction
            assert state_rows(sm) == initial
            assert request.payload.target_id == cmd.actor_id
            assert request.rng_context.version == before.version + 1
            calls.append(request)
            return real_save(request, loader=loader)

        monkeypatch.setattr(evaluation, "evaluate_bound_save", outside_write)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        events = [sm._read_event(sid, seq) for seq in receipt.event_seqs]
        assert [e.event_type for e in events[:3]] == [
            "rules.check_rolled",
            "mechanism.activated",
            "rules.save_rolled",
        ]
        assert events[1].payload["source_event_seq"] == events[0].event_seq
        assert events[2].payload["mechanism_object_id"] == LEVER
        assert events[2].payload["actor_id"] == cmd.actor_id
        assert events[2].payload["engine_event"]["target_id"] == cmd.actor_id
        projected = sm.get_command_rolls(sid, "pick", package, context=context)
        check, save = projected["rolls"]
        assert (check["type"], save["type"]) == ("check_rolled", "save_rolled")
        assert check["succeeded"] is False
        assert save["succeeded"] is (save_dc == 0)
        expected = before.state.restore()
        assert [check["natural"], save["natural"]] == [expected.randint(1, 20) for _ in range(2)]
        after = sm.read_rng_context(sid, package, context=context)
        assert after.state == RNGState.capture(expected)
        assert after.version == before.version + 2
        assert sm._world_facts.light_state(sid, CRED)["enabled"] is (save_dc == 100)
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
        assert all(x[1] for x in sm._world_facts.locks_for_object(sid, DOOR))
        assert len(calls) == 1
        with pytest.raises(StateMachineError):
            sm.get_command_rolls(
                sid, "pick", package, context=TrustedContext("authenticated:bob", "player.bob", ())
            )
        assert sm.submit_command(cmd, package, context=context) == receipt
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.submit_command(cmd, package, context=context) == receipt
        assert reopened.get_command_rolls(sid, "pick", package, context=context) == projected
        assert reopened.read_rng_context(sid, package, context=context) == after
        assert len(calls) == 1


@pytest.mark.parametrize(
    "scenario", ["other_lock", "force", "pick_success", "unprepared", "wrong_tool"]
)
def test_only_actual_failed_pick_of_configured_lock_opens_the_gate(
    tmp_path, context, monkeypatch, scenario
):
    data, loader = pick_failure_data(
        check_dc=0 if scenario == "pick_success" else 100,
        status="unprepared" if scenario == "unprepared" else "armed",
    )
    sm, sid, package, _ = setup_pick(tmp_path, context, data, loader)

    def unexpected_save(*args, **kwargs):
        pytest.fail("The exact lock failure gate must stay closed")

    monkeypatch.setattr(evaluation, "evaluate_bound_save", unexpected_save)
    with sm:
        before = sm.read_rng_context(sid, package, context=context)
        op = pick_command(
            sm,
            sid,
            package,
            context,
            "other",
            lock="lock.inner" if scenario == "other_lock" else "lock.outer",
            tool="object.invalid" if scenario == "wrong_tool" else CRED,
        )
        if scenario == "force":
            receipt = submit(
                sm, sid, package, context, ForceOpen(A, DOOR, "break-outer", None, "0,0"), "other"
            )
        else:
            receipt = sm.submit_command(op, package, context=context)
        rolls = sm.get_command_rolls(sid, "other", package, context=context)["rolls"]
        if scenario == "wrong_tool":
            assert receipt.status == "rejected", receipt
            assert rolls == []
            assert sm.read_rng_context(sid, package, context=context) == before
        else:
            assert receipt.status == "committed", receipt
            (check,) = rolls
            assert check["succeeded"] is (scenario == "pick_success")
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]


@pytest.mark.parametrize(
    "case", ["missing", "not_mechanism", "wrong_operation", "unconfigured", "side"]
)
def test_invalid_failure_reference_is_rejected_in_package_load(tmp_path, case):
    data, _ = pick_failure_data()
    locks = data["world_objects"][0]["capabilities"][1]["locks"]
    lock = locks[0]
    if case == "missing":
        lock["pick_failure_mechanism_id"] = "object.missing"
    elif case == "not_mechanism":
        lock["pick_failure_mechanism_id"] = DOOR
    elif case == "wrong_operation":
        lock["pick_failure_operation_id"] = "nonexistent"
    elif case == "unconfigured":
        lock.update(pick_policy="unconfigured", pick_dc=None)
    else:
        lock["operable_sides"] = [data["scenes"][1]["scene_id"]]
    loaded = write_package(tmp_path, data)
    assert loaded.status == "invalid"
    assert loaded.errors[0].path.endswith("pick_failure_mechanism_id")


def test_explicit_operation_disambiguates_existing_save_gates(tmp_path):
    data, _ = pick_failure_data()
    trap = data["world_objects"][-1]["capabilities"][0]
    down = deepcopy(trap["operations"][0])
    down.update(operation_id="down")
    trap["operations"].append(down)
    trap["triggers"].append(
        trigger("object_operation", target_object_id=DOOR, event_type="object.state_changed")
    )
    assert write_package(tmp_path, data).status == "invalid"
    lock = data["world_objects"][0]["capabilities"][1]["locks"][0]
    lock["pick_failure_operation_id"] = "down"
    loaded = write_package(tmp_path, data)
    assert loaded.status == "valid", loaded.errors
    (exact,) = [
        t
        for d in loaded.package.world_objects
        if d.object_id == LEVER
        for t in d.capability("mechanism").triggers
        if t.kind == "check_result"
    ]
    assert exact.operation_id == "down"


def test_failed_pick_save_and_consequence_roll_back_on_late_storage_fault(tmp_path, context):
    data, loader = pick_failure_data()
    sm, sid, package, _ = setup_pick(tmp_path, context, data, loader)
    with sm:
        cmd = pick_command(sm, sid, package, context)
        before = state_rows(sm)
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_pick_receipt BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT, 'late fault'); END"
        )
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(cmd, package, context=context)
        assert failure.value.code == "storage_error"
        assert sm.get_command_receipt(sid, "pick", package, context=context) is None
        assert state_rows(sm) == before
        assert sm.read_noncombat_snapshot(sid, package, context=context) == snapshot
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0
        sm._conn.execute("DROP TRIGGER fail_pick_receipt")
        assert sm.submit_command(cmd, package, context=context).status == "committed"
        assert sm._world_facts.light_state(sid, CRED)["enabled"]


def test_earlier_existing_check_trigger_changes_cover_before_the_save(
    tmp_path, context, monkeypatch
):
    data, loader = pick_failure_data()
    covered = cover_data("2,0", "total", {"kind": "direct"})
    trap = data["world_objects"][-1]
    trap["capabilities"][0]["operations"][0]["save_gate"]["effect_reach"] = {"kind": "direct"}
    next(obj for obj in data["scenes"][0]["objects"] if obj["object_id"] == LEVER)[
        "initial_cell"
    ] = "2,0"
    cover = covered["world_objects"][-1]
    cover_id = cover["object_id"]
    cover["capabilities"][0]["open_properties"] = {"cover": "half"}
    cover["capabilities"].append({"kind": "openable"})
    cover_obj = covered["scenes"][0]["objects"][-1]
    cover_obj["state"] = "closed"
    data["scenes"][0]["objects"].append(cover_obj)
    data["world_objects"].append(cover)
    earlier = "object.earlier"
    data["scenes"][0]["objects"].append(
        {
            "object_id": earlier,
            "state": None,
            "initial_cell": "0,1",
            "movable": False,
            "blocks_passage": False,
        }
    )
    data["world_objects"].append(
        {
            **trap,
            "object_id": earlier,
            "definition_ref": "definition.earlier",
            "capabilities": [
                config(
                    operations=[
                        {
                            "operation_id": "up",
                            "actions": [
                                {
                                    "kind": "set_open_state",
                                    "target_object_id": cover_id,
                                    "value": "open",
                                }
                            ],
                            "consume_on_noop": True,
                        }
                    ],
                    triggers=[trigger("check_result", target_object_id=DOOR, succeeded=False)],
                )
            ],
        }
    )
    sm, sid, package, _ = setup_pick(tmp_path, context, data, loader)
    with sm:
        real_save = evaluation.evaluate_bound_save

        def check_restored_cover(request, *, loader):
            assert not sm._conn.in_transaction
            assert sm._world_facts.object(sid, cover_id)["state"] == "closed"
            return real_save(request, loader=loader)

        monkeypatch.setattr(evaluation, "evaluate_bound_save", check_restored_cover)
        cmd = pick_command(sm, sid, package, context)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        events = [sm._read_event(sid, seq) for seq in receipt.event_seqs]
        activated = [
            e.payload["object_id"] for e in events if e.event_type == "mechanism.activated"
        ]
        assert activated == [earlier, LEVER]
        _, save = sm.get_command_rolls(sid, "pick", package, context=context)["rolls"]
        assert [m["value"] for m in save["roll_detail"]["modifiers"] if m["source"] == "cover"] == [
            2
        ]
        assert sm._world_facts.object(sid, cover_id)["state"] == "open"


def test_pick_failure_reads_ignore_remote_growth(tmp_path, context, record_property):
    from test_faerie_fire import _grow_remote
    from test_scoped_runtime_validation import measure

    counts = []
    for size in (0, 25):
        case = tmp_path / f"pick-{size}"
        case.mkdir()
        data, loader = pick_failure_data()
        sm, sid, package, _ = setup_pick(case, context, data, loader)
        with sm:
            _grow_remote(sm, sid, size)
            cmd = pick_command(sm, sid, package, context)
            with measure(sm) as (work, _):
                assert sm.submit_command(cmd, package, context=context).status == "committed"
            counts.append((work["sql_selects"], work["sql_rows"]))
    record_property("sql_selects_and_rows_for_0_and_25_remote_pairs", str(counts))
    assert counts[0] == counts[1], counts


def inspired_game(tmp_path, context, *, save_dc):
    """Use the canonical Bardic grant asset with the lock's reviewed tool binding."""
    from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
    from dnd5e_srd_data import MemoryAssetLoader

    data, base = pick_failure_data(save_dc=save_dc)
    loader = MemoryAssetLoader(
        items=[base.get_item(slug) for slug in base.list_slugs("items")],
        monsters=[base.get_monster(slug) for slug in base.list_slugs("monsters")],
        features=[get_default_ruleset().get_feature("bardic-inspiration")],
    )
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    return *setup_pick(tmp_path, context, data, loader), loader


@pytest.mark.parametrize("option", ["accept", "decline"])
def test_failed_pick_inspiration_keeps_both_original_rolls(tmp_path, context, option):
    from test_mechanism_save_gate import grant_bardic

    from trpg_state_machine.choice_models import ChoiceRespondOperationPayload
    from trpg_state_machine.commands import TypedCommand

    sm, sid, package, db, loader = inspired_game(tmp_path, context, save_dc=100)
    with sm:
        cmd = pick_command(sm, sid, package, context)
        grant_bardic(sm, sid, cmd.actor_id)
        before = state_rows(sm)
        rng = sm.read_rng_context(sid, package, context=context)
        pending = sm.submit_command(cmd, package, context=context)
        assert pending.status == "pending_choice", pending
        assert state_rows(sm) == before
        assert sm.submit_command(cmd, package, context=context) == pending
        first_check, first_save = sm.get_command_rolls(sid, "pick", package, context=context)[
            "rolls"
        ]
        assert (first_check["type"], first_save["type"]) == ("check_rolled", "save_rolled")
        answer = TypedCommand(
            sid,
            "answer",
            cmd.actor_id,
            "choice.respond",
            ChoiceRespondOperationPayload("pick", option),
            pending.world_version,
        )
        if option == "accept":
            sm._conn.execute(
                "CREATE TEMP TRIGGER fail_pick_reply BEFORE INSERT ON command_receipts "
                "WHEN NEW.command_id='answer' BEGIN SELECT RAISE(ABORT, 'late fault'); END"
            )
            with pytest.raises(StateMachineError) as fault:
                sm.submit_command(answer, package, context=context)
            assert fault.value.code == "storage_error"
            assert state_rows(sm) == before
            assert sm.get_command_receipt(sid, "answer", package, context=context) is None
            assert sm.get_command_receipt(sid, "pick", package, context=context) == pending
            sm._conn.execute("DROP TRIGGER fail_pick_reply")
        receipt = sm.submit_command(answer, package, context=context)
        assert receipt.status == "committed", receipt
        check, save = sm.get_command_rolls(sid, "pick", package, context=context)["rolls"]
        assert [first_check["natural"], first_save["natural"]] == [
            check["natural"],
            save["natural"],
        ]
        expected = rng.state.restore()
        assert [check["natural"], save["natural"]] == [expected.randint(1, 20) for _ in range(2)]
        if option == "accept":
            expected.randint(1, 8)
        assert sm.read_rng_context(sid, package, context=context).state == RNGState.capture(
            expected
        )
        assert sm._world_facts.light_state(sid, CRED)["enabled"]
        remaining = sm._world.characters.effects.held_bardic_inspiration(sid, cmd.actor_id, 0)
        assert (remaining is None) == (option == "accept")
        assert sm.submit_command(answer, package, context=context) == receipt
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.submit_command(answer, package, context=context) == receipt
        assert reopened.get_command_rolls(sid, "pick", package, context=context)["rolls"] == [
            check,
            save,
        ]


def test_successful_trap_save_preserves_held_inspiration(tmp_path, context):
    from test_mechanism_save_gate import grant_bardic

    sm, sid, package, _, _ = inspired_game(tmp_path, context, save_dc=0)
    with sm:
        cmd = pick_command(sm, sid, package, context)
        grant_bardic(sm, sid, cmd.actor_id)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        check, save = sm.get_command_rolls(sid, "pick", package, context=context)["rolls"]
        assert not check["succeeded"] and save["succeeded"]
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
        assert sm._world.characters.effects.held_bardic_inspiration(sid, cmd.actor_id, 0)
