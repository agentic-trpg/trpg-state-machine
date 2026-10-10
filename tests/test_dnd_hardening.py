import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import yaml
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, PreflightChoice
from dnd5e_engine.evaluation_state import CombatSnapshot, NonCombatSnapshot
from dnd_support import NPC, loader
from dnd_support import package_data as dnd_package_data
from pydantic import ValidationError
from test_dnd_npc_availability import npc_command
from test_dnd_runtime import attack, authority, setup

from trpg_state_machine import (
    EnterScene,
    MoveActor,
    ObjectState,
    SetObjectState,
    StateMachine,
    StateMachineError,
)
from trpg_state_machine.perception import NPCContext


def test_real_vertical_pipeline_committed_observations_fresh_process_audit(
    tmp_path, package_data, context, monkeypatch
):
    from dnd5e_engine import orchestrator

    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy registry/AI entry used")

    for name in ("start_combat", "_get_live", "advance_monster_turn", "rank_monster_actions"):
        monkeypatch.setattr(orchestrator, name, forbidden)
    sm, sid, package, assets = setup(tmp_path, package_data, context)
    path = tmp_path / "package.yaml"
    path.write_text(yaml.safe_dump(dnd_package_data(package_data, assets)), encoding="utf-8")
    with sm:
        sm.grant_npc_access(sid, NPC, "agent:lantern", package, context=context)
        pc = attack(sm, sid, package, context)
        assert sm.submit_command(pc, package, context=context).status == "committed"
        npc = npc_command(sm, sid, package, context)
        assert sm.submit_command(npc, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        delivered = []
        for event in sm.read_events(sid, package, context=context):
            assert sm.project_event(sid, event.event_seq, package, context=context) == "resolved"
            assert sm.deliver_event(
                sid, event.event_seq, package, context=context, sink=delivered.append
            )
        assert sm.delivery_cursor(sid, package, context=context) == sm.perception_cursor(
            sid, package, context=context
        )
        assert sm._conn.execute("SELECT count(*) FROM event_outbox WHERE done=0").fetchone()[0] == 0
        observations = sm.read_observations(
            sid, package, context=NPCContext("agent:lantern", sid, NPC)
        )
        contents = [[s.content for s in o.perceived_signals] for o in observations]
        assert sum(c == ["Someone makes an attack."] for c in contents) == 2
        public = json.dumps(contents)
        for hidden in ("damage_applied", "hp_current", "rng", "roll_total", "weapon_id"):
            assert hidden not in public
        bad = replace(
            attack(sm, sid, package, context, "attack.bad"),
            payload=CombatIntentPayload(
                intent_type="attack", weapon_id="item.lantern_blade", target_id="npc.absent"
            ),
        )
        event_count = sm._conn.execute("SELECT count(*) FROM events").fetchone()[0]
        assert sm.submit_command(bad, package, context=context).status == "rejected"
        assert sm._conn.execute("SELECT count(*) FROM events").fetchone()[0] == event_count
        assert (
            sm.read_observations(sid, package, context=NPCContext("agent:lantern", sid, NPC))
            == observations
        )
        with pytest.raises(StateMachineError) as caught:
            sm.save_session(sid, package, context=context)
        assert caught.value.code == "combat_unrestorable"
        cmd = asdict(npc)
        cmd["payload"] = npc.payload.model_dump(mode="json")
        command_path = tmp_path / "command.json"
        command_path.write_text(json.dumps(cmd), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("_dnd_restart_worker.py")),
            str(tmp_path / "dnd.db"),
            str(path),
            str(command_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    actual = json.loads(completed.stdout)
    assert CombatSnapshot.model_validate_json(json.dumps(actual["snapshot"])) == before
    assert actual["rng"] == json.loads(rng.model_dump_json())
    assert actual["receipt"] == npc.command_id
    assert actual["failures"] == ["combat_unrestorable"] * 3
    assert actual["observations"] == contents and not actual["new_receipt"]
    with sqlite3.connect(tmp_path / "dnd.db") as db:
        assert (
            db.execute("SELECT world_version FROM sessions WHERE session_id=?", (sid,)).fetchone()[
                0
            ]
            == before.world_version
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "read_set",
        "input_world_version",
        "proposed_events",
        "rng_transition",
        "input_ruleset_binding",
    ],
)
def test_missing_required_result_fields_never_persist_rejection(
    tmp_path, package_data, context, field
):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        before = authority(sm, sid)
        real = sm._evaluate_dnd

        async def invalid(request):
            result = await real(request)
            result.__dict__.pop(field)
            return result

        sm._evaluate_dnd = invalid
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(command, package, context=context)
        assert caught.value.code == "invalid_rule_result"
        assert authority(sm, sid) == before
        sm._evaluate_dnd = real
        assert sm.submit_command(command, package, context=context).status == "committed"


def test_unexpected_choice_is_independent_protocol_error(tmp_path, package_data, context):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        before = authority(sm, sid)
        real = sm._evaluate_dnd

        async def unexpected(request):
            result = await real(request)
            return result.model_copy(
                update={
                    "status": "needs_choice",
                    "state_delta": None,
                    "rng_transition": None,
                    "proposed_events": (),
                    "choice": PreflightChoice(
                        kind="attack.weapon",
                        actor_id=command.actor_id,
                        allowed_weapon_ids=("item.lantern_blade",),
                    ),
                }
            )

        sm._evaluate_dnd = unexpected
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(command, package, context=context)
        assert caught.value.code == "invalid_rule_result"
        assert authority(sm, sid) == before
        sm._evaluate_dnd = real
        assert sm.submit_command(command, package, context=context).status == "committed"


def test_rng_revalidation_detects_independent_stream_conflict(tmp_path, package_data, context):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        real = sm._evaluate_dnd

        async def concurrent(request):
            result = await real(request)
            assert not sm._conn.in_transaction
            sm._conn.execute(
                "UPDATE rng_states SET rng_version=rng_version+1 WHERE session_id=?", (sid,)
            )
            return result

        sm._evaluate_dnd = concurrent
        snapshot = sm.read_combat_snapshot(sid, package, context=context)
        original = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("conflict", "version_conflict")
        assert sm.read_combat_snapshot(sid, package, context=context) == snapshot
        current = sm.read_rng_context(sid, package, context=context)
        assert current.state == original.state and current.version == original.version + 1
        assert receipt.event_seqs == ()


@pytest.mark.parametrize(
    "operation,kind",
    [
        (EnterScene("scene.workshop"), "enter_scene"),
        (MoveActor(NPC, "scene.workshop"), "move_actor"),
        (
            SetObjectState("scene.platform", "object.platform_gate", ObjectState.OPEN),
            "set_object_state",
        ),
    ],
)
def test_unrepresented_combat_world_mutations_are_refused(
    tmp_path, package_data, context, operation, kind
):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        command = replace(
            command,
            command_type=kind,
            payload=operation,
            actor_id=NPC if kind == "move_actor" else command.actor_id,
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        world = sm.read_session(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.error_code == "unsupported_combat_world_operation"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_session(sid, package, context=context) == world


def test_noncombat_discriminator_is_not_forged_into_combat(tmp_path, package_data, context):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        data = sm.read_combat_snapshot(sid, package, context=context).model_dump(mode="json")
        data["snapshot_kind"] = "non_combat"
        with pytest.raises(ValidationError):
            NonCombatSnapshot.model_validate_json(json.dumps(data))
        del data["combat_state"]
        actual = NonCombatSnapshot.model_validate_json(json.dumps(data))
        assert actual.snapshot_kind == "non_combat" and actual.character_states
        with pytest.raises(ValidationError):
            CombatSnapshot.model_validate_json(actual.model_dump_json())


@pytest.mark.parametrize(
    "legacy_schema,commit",
    [
        (7, "8f24ca03e6037428bbc559d50e91bcd57669a0ed"),
        (8, "a00854374a38b01a13cc0fbfa089ef6e0b709cef"),
    ],
)
def test_actual_legacy_combat_upgrade_preserves_mechanics_rng_receipt(
    tmp_path, package_data, context, legacy_python, legacy_schema, commit
):
    source = tmp_path / "sm7-source"
    archived = subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            commit,
            "src",
            "tests/fixtures",
            "tests/dnd_support.py",
        ],
        capture_output=True,
        check=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archived)) as contents:
        contents.extractall(source)
    database = tmp_path / "real-old.db"
    worker = Path(__file__).with_name("_dnd_legacy_worker.py")
    completed = subprocess.run(
        [legacy_python, str(worker), str(source), str(database), str(legacy_schema)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    old = json.loads(completed.stdout)
    from test_migrations import stored_rows

    original = stored_rows(database)
    assets = loader()
    from trpg_state_machine.package import _validate

    package = _validate(old["package_data"])
    sid = old["session_id"]
    with StateMachine(database, dnd_loader=assets) as sm:
        snapshot = sm.read_combat_snapshot(sid, package, context=context)
        assert snapshot == CombatSnapshot.model_validate_json(json.dumps(old["snapshot"]))
        assert (
            json.loads(sm.read_rng_context(sid, package, context=context).model_dump_json())
            == old["rng"]
        )
        assert (
            sm.get_command_receipt(sid, "old.real.attack", package, context=context).status
            == "committed"
        )
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 14
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        before = sm.read_session(sid, package, context=context)
        assert before.ruleset_binding.evaluator_version == "dnd5e-evaluation/4"
        command = attack(sm, sid, package, context, key="old.unsupported")
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(command, package, context=context)
        assert caught.value.code == "ruleset_binding_mismatch"
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sm.read_session(sid, package, context=context) == before
    with sqlite3.connect(database) as db:
        for table, (columns, rows) in original.items():
            assert rows <= set(db.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall())


def test_npc_zero_hp_delta_keeps_condition_and_damage_ledger_consistent(
    tmp_path, package_data, context
):
    base = deepcopy(package_data)
    base["pc_templates"][0]["character"]["hp"]["current"] = 1

    def change(data):
        data["snapshot"]["character_states"][0]["hp_current"] = 1
        data["snapshot"]["combat_state"].update(
            current_turn_index=1, turn_serial=2, monster_turn_start_done=[2, NPC]
        )

    sm, sid, package, _ = setup(tmp_path, base, context, change)
    with sm:
        command = npc_command(sm, sid, package, context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        pc = after.character_states[0]
        assert pc.hp_current == 0 and pc.is_alive
        assert any(c.condition == "unconscious" for c in pc.conditions)
        assert any(
            actor == pc.entity_id
            for actor, _ in after.combat_state.processed_zero_hp_damage_instances
        )
        assert (
            sm._conn.execute(
                "SELECT current_hp FROM characters WHERE actor_id=?", (pc.entity_id,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("fault", ["prevalue", "duplicate", "unknown_operation", "config_changed"])
def test_delta_and_configuration_faults_remain_retryable(tmp_path, package_data, context, fault):
    from dnd5e_engine.evaluation_delta import HPDelta, StateDelta
    from dnd5e_srd_data import MemoryAssetLoader

    sm, sid, package, assets = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        before = authority(sm, sid)
        real = sm._evaluate_dnd

        async def broken(request):
            result = await real(request)
            if fault == "config_changed":
                sm._dnd_loader = MemoryAssetLoader()
                return result
            operations = list(result.state_delta.operations)
            if fault == "prevalue":
                index = next(i for i, op in enumerate(operations) if isinstance(op, HPDelta))
                old = operations[index]
                operations[index] = old.model_copy(
                    update={
                        "expected_hp": old.expected_hp + 1,
                        "resulting_hp": old.resulting_hp + 1,
                    }
                )
            elif fault == "duplicate":
                operations.append(operations[0])
            else:
                return result.model_copy(
                    update={
                        "state_delta": StateDelta.model_construct(
                            expected_world_version=request.state_snapshot.world_version,
                            operations=({"kind": "arbitrary.sql"},),
                        )
                    }
                )
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    )
                }
            )

        sm._evaluate_dnd = broken
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(command, package, context=context)
        assert caught.value.code in {"invalid_rule_result", "ruleset_binding_mismatch"}
        assert authority(sm, sid) == before
        sm._dnd_loader = assets
        sm._evaluate_dnd = real
        assert sm.submit_command(command, package, context=context).status == "committed"
