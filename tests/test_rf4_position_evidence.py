"""RF4-REV-01: real public producers, current fact owners and frozen evidence."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from dnd_support import NPC, create, initialize
from modern_support import modern_data, modern_loader
from perception_support import synthetic_notice
from test_combat_closure import close_command, pc_attack
from test_events import event_data
from test_migrations import assert_original_rows_equal, stored_rows

from trpg_state_machine import (
    EnterScene,
    SetSceneFlag,
    StateMachine,
    StateMachineError,
    StepActor,
    TypedCommand,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.perception_conditions import judge_signal, signal_environment

A, B = "scene.platform", "scene.workshop"


def authored_package(base, *, spatial=False, nontrivial=False, origin_cell=None):
    assets = modern_loader()
    data = modern_data(base, assets, spatial=spatial)
    if nontrivial:
        # Bounded combat initialization admits plain bright geometry only.
        # Nontrivial sensory admission uses the supported noncombat foundation.
        del data["rule_bindings"]["combat_foundation"]
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"][
            "blocked_cells"
        ] = ["2,2"]
    definition = event_data(
        "event.position_notice",
        {"kind": "set_scene_flag", "flag_id": "flag.lamp_checked", "value": False},
    )
    definition["signals"][0].update(
        signal_id="signal.activity",
        channel="visual",
        content="The caretaker gestures.",
        origin={"scene_id": A, "entity_id": NPC},
        stimulus={"kind": "ordinary_activity"},
    )
    if origin_cell is not None:
        definition["signals"][0]["origin"]["cell"] = origin_cell
    data["events"] = [definition]
    data["scenes"][0]["event_definition_ids"] = [definition["event_definition_id"]]
    return _validate(data), assets


def command(sm, sid, package, context, kind, payload, key):
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(sid, key, state.pc_actor_id, kind, payload, state.world_version)


def fire(sm, sid, package, context, key="fire.position"):
    cmd = command(
        sm, sid, package, context, "set_scene_flag", SetSceneFlag(A, "flag.lamp_checked", True), key
    )
    receipt = sm.submit_command(cmd, package, context=context)
    assert receipt.status == "committed"
    event = next(
        e
        for e in sm.read_events(sid, package, context=context)
        if e.event_seq in receipt.event_seqs and e.event_type == "package.event_fired"
    )
    return cmd, receipt, event


def saved_evidence(db, sid, seq):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
            (sid, seq),
        ).fetchone()[0]


@pytest.mark.parametrize("spatial", [False, True])
def test_active_combat_package_event_captures_actual_observer_position(
    tmp_path, package_data, context, spatial
):
    package, assets = authored_package(package_data, spatial=spatial)
    db = tmp_path / "active.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        assert sm._conn.execute(
            "SELECT spatial_enabled FROM mechanical_scenes WHERE session_id=? AND scene_id=?",
            (sid, A),
        ).fetchone()[0] == int(spatial)
        positions = dict(
            sm._conn.execute(
                "SELECT actor_id,cell FROM actors WHERE session_id=? AND current_scene_id=?",
                (sid, A),
            )
        )
        assert positions[NPC] == "1,0"
        assert "actor_zone" not in json.loads(
            sm._conn.execute(
                "SELECT state_json FROM combats WHERE session_id=?", (sid,)
            ).fetchone()[0]
        )
        assert (
            sm.read_combat_snapshot(sid, package, context=context).combat_state.actor_zone
            == positions
        )
        _, _, event = fire(sm, sid, package, context)
        evidence = json.loads(saved_evidence(db, sid, event.event_seq))
        assert evidence["schema"] == ("event-evidence/5" if spatial else "event-evidence/4")
        assert evidence["phase"] == {"status": "captured", "code": "package.after_effect"}
        observer = next(a for a in evidence["actors"] if a["actor_id"] == NPC)
        assert observer["mechanical"]["cell"] == positions[NPC]
        assert signal_environment(event.signals[0], evidence)["origin_cell"] == positions[NPC]
        if spatial:
            assert observer["cell"] == positions[NPC]


@pytest.mark.parametrize("spatial", [False, True])
def test_public_departure_captures_observer_and_source_actor_positions(
    tmp_path, package_data, context, spatial
):
    package, assets = authored_package(package_data, spatial=spatial)
    db = tmp_path / "source.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        # Departure is a reachable proven stage before combat. Active combat
        # deliberately refuses movement; Package origins cannot name dynamic PCs.
        cmd = command(sm, sid, package, context, "enter_scene", EnterScene(B), "depart")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed"
        event = next(
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_seq in receipt.event_seqs and e.event_type == "scene.exited"
        )
        evidence = json.loads(saved_evidence(db, sid, event.event_seq))
        assert evidence["phase"]["code"] == "scene.before_departure"
        observer = next(a for a in evidence["actors"] if a["actor_id"] == NPC)
        source = next(a for a in evidence["source_actors"] if a["actor_id"] == pc)
        assert (observer["mechanical"]["cell"], source["mechanical"]["cell"]) == ("1,0", "0,0")
        assert signal_environment(event.signals[0], evidence)["origin_cell"] == "0,0"
        assert sm.read_actor_position(sid, pc, package, context=context).scene_id == B


@pytest.mark.parametrize("role", ["observer", "source"])
@pytest.mark.parametrize("cell", ["99,0", "01,0", "not-a-cell", "2,2"])
def test_invalid_current_cells_refuse_atomically(tmp_path, package_data, context, role, cell):
    package, assets = authored_package(package_data, nontrivial=True)
    db = tmp_path / "invalid.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        # Explicit corruption injection, never an alternative success path.
        sm._conn.execute(
            "UPDATE actors SET cell=? WHERE session_id=? AND actor_id=?",
            (cell, sid, NPC if role == "observer" else pc),
        )
        cmd = command(sm, sid, package, context, "enter_scene", EnterScene(B), "invalid.depart")
        before = stored_rows(db)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "rejected" and receipt.error_code == "invalid_spatial_state"
        after = stored_rows(db)
        # Domain refusal rolls back its savepoint and commits only the refusal
        # receipt; storage faults below roll back even that outer receipt.
        assert {k: v for k, v in after.items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert stored_rows(db) == after


@pytest.mark.parametrize("fault", ["missing_cell", "missing_grid", "wrong_scene", "movement_only"])
def test_missing_proven_geometry_cannot_be_filled_by_judgment(
    tmp_path, package_data, context, fault
):
    package, assets = authored_package(package_data, nontrivial=True)
    db = tmp_path / "unknown.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        # This valid noncombat Session has complete nontrivial geometry. Faults
        # remove/contradict facts; no unsupported combat setup is being admitted.
        if fault == "missing_cell":
            sm._conn.execute(
                "UPDATE actors SET cell=NULL WHERE session_id=? AND actor_id=?", (sid, NPC)
            )
        elif fault == "missing_grid":
            sm._conn.execute(
                "DELETE FROM mechanical_scenes WHERE session_id=? AND scene_id=?", (sid, A)
            )
        else:
            raw = json.loads(
                sm._conn.execute(
                    "SELECT state_json FROM mechanical_scenes WHERE session_id=? AND scene_id=?",
                    (sid, A),
                ).fetchone()[0]
            )
            if fault == "wrong_scene":
                raw["scene_id"] = B
            else:
                raw["grid"] = {
                    k: raw["grid"][k] for k in ("width", "height", "cell_size_ft", "blocked_cells")
                }
            sm._conn.execute(
                "UPDATE mechanical_scenes SET state_json=? WHERE session_id=? AND scene_id=?",
                (json.dumps(raw), sid, A),
            )
        _, _, event = fire(sm, sid, package, context)
        evidence = json.loads(saved_evidence(db, sid, event.event_seq))
        observer = next(a for a in evidence["actors"] if a["actor_id"] == NPC)
        assert observer["mechanical"]["cell"] is None
        if fault != "missing_cell":
            assert evidence["scenes"][A]["grid"] is None
        decision = judge_signal(
            event.signals[0], observer, signal_environment(event.signals[0], evidence)
        )
        assert decision.outcome == "unresolved" and not decision.judgment_allowed
        assert (
            sm.project_event(
                sid,
                event.event_seq,
                package,
                context=context,
                judgment=lambda _: pytest.fail("Cannot invent required geometry"),
            )
            == "unresolved"
        )
        assert sm._conn.execute("SELECT count(*) FROM event_observations").fetchone()[0] == 0
        assert (
            sm._conn.execute(
                "SELECT complete FROM npc_perception_queue "
                "WHERE session_id=? AND npc_id=? AND event_seq=?",
                (sid, NPC, event.event_seq),
            ).fetchone()[0]
            == 0
        )


def test_plain_bright_missing_position_retains_general_judgment(tmp_path, package_data, context):
    package, assets = authored_package(package_data)
    db = tmp_path / "plain.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        sm._conn.execute(
            "UPDATE actors SET cell=NULL WHERE session_id=? AND actor_id=?", (sid, NPC)
        )
        _, _, event = fire(sm, sid, package, context)
        evidence = json.loads(saved_evidence(db, sid, event.event_seq))
        observer = next(a for a in evidence["actors"] if a["actor_id"] == NPC)
        decision = judge_signal(
            event.signals[0], observer, signal_environment(event.signals[0], evidence)
        )
        assert decision.reason == "missing_spatial_evidence" and decision.judgment_allowed
        assert observer["mechanical"]["cell"] is None
        assert (
            sm.project_event(
                sid, event.event_seq, package, context=context, judgment=synthetic_notice
            )
            == "resolved"
        )


def test_declared_origin_mismatch_does_not_override_actual_position(
    tmp_path, package_data, context
):
    package, assets = authored_package(package_data, origin_cell="2,0")
    db = tmp_path / "mismatch.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        _, _, event = fire(sm, sid, package, context)
        evidence = json.loads(saved_evidence(db, sid, event.event_seq))
        assert evidence["actors"][0]["mechanical"]["cell"] == "1,0"
        assert signal_environment(event.signals[0], evidence)["origin_valid"] is False
        assert (
            sm.project_event(
                sid,
                event.event_seq,
                package,
                context=context,
                judgment=lambda _: pytest.fail("Declared origin contradicts actual facts"),
            )
            == "unresolved"
        )


@pytest.mark.parametrize("spatial", [False, True])
def test_evidence_survives_close_movement_reopen_and_historical_projection(
    tmp_path, package_data, context, monkeypatch, spatial
):
    package, assets = authored_package(package_data, spatial=spatial)
    db = tmp_path / "frozen.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        _, _, event = fire(sm, sid, package, context)
        frozen = saved_evidence(db, sid, event.event_seq)
        assert (
            sm.submit_command(pc_attack(sm, sid, package, context), package, context=context).status
            == "committed"
        )
        assert (
            sm.submit_command(
                close_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        terminal = sm.read_combat_snapshot(
            sid, package, context=context
        ).combat_state.actor_zone.copy()
        assert terminal[pc] == "0,0"
        payload, kind = (
            (StepActor(A, "0,0", "0,1"), "world.step_actor")
            if spatial
            else (EnterScene(B), "enter_scene")
        )
        receipt = sm.submit_command(
            command(sm, sid, package, context, kind, payload, "after.close"),
            package,
            context=context,
        )
        assert receipt.status == "committed"
        moved = next(
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_seq in receipt.event_seqs
            and e.event_type in ("actor.stepped", "scene.entered")
        )
        current = json.loads(saved_evidence(db, sid, moved.event_seq))
        source = next(a for a in current["source_actors"] if a["actor_id"] == pc)
        assert source["mechanical"]["cell"] == ("0,1" if spatial else None)
        assert source["mechanical"]["cell"] != terminal[pc]
        assert (
            sm.read_combat_snapshot(sid, package, context=context).combat_state.actor_zone
            == terminal
        )
        assert saved_evidence(db, sid, event.event_seq) == frozen
    with StateMachine(db, dnd_loader=assets) as sm:
        assert saved_evidence(db, sid, event.event_seq) == frozen
        monkeypatch.setattr(
            sm._characters,
            "position",
            lambda *args: pytest.fail("Past evidence cannot read current positions"),
        )
        traced = []
        sm._conn.set_trace_callback(traced.append)

        def notice(request):
            assert request.evidence["observer"]["mechanical"]["cell"] == "1,0"
            assert request.evidence["observer"]["mechanical"]["is_alive"] is True
            return synthetic_notice(request)

        assert (
            sm.project_event(sid, event.event_seq, package, context=context, judgment=notice)
            == "resolved"
        )
        sm._conn.set_trace_callback(None)
        assert not any(
            "from mechanical_scenes" in q.lower()
            or "from mechanical_actors" in q.lower()
            or "select current_scene_id,cell from actors" in q.lower()
            for q in traced
        )
        assert saved_evidence(db, sid, event.event_seq) == frozen
        assert (
            sm.project_event(sid, event.event_seq, package, context=context) == "already_complete"
        )


@pytest.mark.parametrize("table", ["event_evidence", "event_outbox"])
def test_evidence_or_outbox_fault_rolls_back_all_facts_and_same_command_retries(
    tmp_path, package_data, context, table
):
    package, assets = authored_package(package_data)
    db = tmp_path / "atomic.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        cmd = command(
            sm,
            sid,
            package,
            context,
            "set_scene_flag",
            SetSceneFlag(A, "flag.lamp_checked", True),
            "fault.retry",
        )
        sm._conn.execute(
            f"CREATE TRIGGER fail_position_evidence BEFORE INSERT ON {table} "
            "WHEN EXISTS (SELECT 1 FROM events WHERE session_id=NEW.session_id "
            "AND event_seq=NEW.event_seq AND event_type='package.event_fired') "
            "BEGIN SELECT RAISE(ABORT,'position evidence fault'); END"
        )
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(cmd, package, context=context)
        assert error.value.code == "storage_error" and stored_rows(db) == before
        assert sm.get_command_receipt(sid, cmd.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_position_evidence")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed"
        event = next(
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_seq in receipt.event_seqs and e.event_type == "package.event_fired"
        )
        assert (
            json.loads(saved_evidence(db, sid, event.event_seq))["actors"][0]["mechanical"]["cell"]
            == "1,0"
        )
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_outbox WHERE session_id=? AND event_seq=?",
                (sid, event.event_seq),
            ).fetchone()[0]
            == 2
        )
        assert (
            sm._conn.execute(
                "SELECT complete FROM npc_perception_queue "
                "WHERE session_id=? AND npc_id=? AND event_seq=?",
                (sid, NPC, event.event_seq),
            ).fetchone()[0]
            == 0
        )
        committed = stored_rows(db)
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert stored_rows(db) == committed


def test_actual_baseline_missing_evidence_is_preserved_without_automatic_repair(
    tmp_path, package_data, context
):
    root = Path(__file__).resolve().parents[1]
    baseline = "4e4e678e1b2e580959fc22d07465940211c66a3a"
    old = tmp_path / "baseline"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", baseline, "src"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        files.extractall(old)
    base = tmp_path / "base.json"
    base.write_text(json.dumps(package_data), "utf-8")
    db = tmp_path / "old.db"
    script = """
import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import trpg_state_machine.event_capture as capture
assert Path(capture.__file__).is_relative_to(Path(sys.argv[4]))
from test_rf4_position_evidence import authored_package, create, initialize, fire
from trpg_state_machine import StateMachine, TrustedContext
package, assets = authored_package(json.loads(Path(sys.argv[2]).read_text('utf-8')))
context = TrustedContext('authenticated:alice', 'player.alice', ('npc.caretaker','npc.attendant'))
with StateMachine(sys.argv[3], dnd_loader=assets) as sm:
 sid = create(sm, package, context)
 assert initialize(sm, sid, package, context).status == 'committed'
 _, _, event = fire(sm, sid, package, context)
 print(json.dumps([sid,event.event_seq]))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(root / "tests"), str(base), str(db), str(old)],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(old / "src")),
        capture_output=True,
        text=True,
        check=True,
    )
    sid, seq = json.loads(process.stdout)
    frozen = saved_evidence(db, sid, seq)
    assert json.loads(frozen)["actors"][0]["mechanical"]["cell"] is None
    before = stored_rows(db)
    package, assets = authored_package(package_data)
    with StateMachine(db, dnd_loader=assets) as sm:
        assert_original_rows_equal(before, stored_rows(db))
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM objects WHERE carrier_id IS NOT NULL OR item_json IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert sm.read_actor_position(sid, NPC, package, context=context).cell == "1,0"
        assert (
            sm.get_command_receipt(sid, "fire.position", package, context=context).status
            == "committed"
        )
        assert sm.project_event(sid, seq, package, context=context) == "unresolved"
        assert saved_evidence(db, sid, seq) == frozen
        assert (
            sm._conn.execute(
                "SELECT complete FROM npc_perception_queue "
                "WHERE session_id=? AND npc_id=? AND event_seq=?",
                (sid, NPC, seq),
            ).fetchone()[0]
            == 0
        )
