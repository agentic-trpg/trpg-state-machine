"""Real SQLite NPC ownership, provenance, isolation, rollback and fresh-process restore."""

import copy
import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Barrier

import pytest
import yaml
from perception_support import synthetic_notice
from test_migrations import assert_preserved, historical_database, stored_rows

from trpg_state_machine import (
    CreateSessionRequest,
    EnterScene,
    MoveActor,
    PlayerSelection,
    StateMachine,
    StateMachineError,
    load_adventure_package,
)
from trpg_state_machine.perception import NPCContext

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests/fixtures/npc_station.yaml"
NPC_TABLES = (
    "npc_states",
    "world_facts",
    "npc_known_facts",
    "npc_private_records",
    "npc_record_sources",
    "npc_plan_goals",
)


@pytest.fixture
def npc_package():
    loaded = load_adventure_package(SOURCE)
    assert loaded.status == "valid", loaded.errors
    return loaded.package


def request(package, context, key="create.npc"):
    return CreateSessionRequest(
        key,
        package.adventure_id,
        package.package_version,
        package.package_digest,
        (PlayerSelection(context.player_id, "pc_template.observer"),),
    )


def grant(sm, sid, package, context, npc="npc.caretaker"):
    sm.grant_npc_access(sid, npc, "agent:" + npc, package, context=context)
    return NPCContext("agent:" + npc, sid, npc)


def view(sm, sid, package, npc="npc.caretaker"):
    return sm.get_npc_view(sid, npc, package, context=NPCContext("agent:" + npc, sid, npc))


def rows(database, tables=NPC_TABLES):
    with sqlite3.connect(database) as db:
        return {t: db.execute(f"SELECT * FROM {t} ORDER BY 1,2,3").fetchall() for t in tables}


def enter(sm, sid, package, context, scene):
    before = sm.read_session(sid, package, context=context)
    return sm._apply(
        sid,
        package,
        EnterScene(scene),
        expected_world_version=before.world_version,
        context=context,
    )


def test_complete_initial_private_view_and_belief_history(tmp_path, npc_package, context):
    db = tmp_path / "npc.db"
    with StateMachine(db) as sm:
        sid = sm.create_session(
            request(npc_package, context), npc_package, context=context
        ).session_id
        grant(sm, sid, npc_package, context)
        result = view(sm, sid, npc_package)
        assert result.npc_id == "npc.caretaker"
        assert result.name == "Synthetic Caretaker"
        assert result.controller_kind == "sub_agent"
        assert result.configuration == "configured"
        assert result.initialization_origin == "package"
        assert result.npc_state_version == 1
        assert result.schema_version == "npc-state/1"
        assert result.package_digest == npc_package.package_digest
        assert result.profile.role == "lamp caretaker"
        assert result.profile.personality == ("patient", "cautious")
        assert result.profile.speaking_style == "Short, precise sentences."
        assert result.profile.constraints == ("Keep private records private.",)
        assert result.memories[0].text == "Caretaker private backstory."
        assert result.memories[0].epistemic_status == "told"
        assert [(b.version, b.is_current, b.status) for b in result.belief_history] == [
            (1, False, "active"),
            (2, True, "retracted"),
        ]
        assert result.belief_history[0].confidence_basis_points == 7000
        assert result.belief_history[0].sources[0].kind == "package"
        assert result.belief_history[0].sources[0].reference_id == result.npc_id
        assert result.belief_history[1].sources[0].reference_id == "memory.backstory"
        assert result.belief_history[1].sources[0].version == 1
        assert result.belief_history[1].confidence_basis_points is None
        assert result.belief_history[0].epistemic_status == "inferred"
        assert result.known_facts[0].text == "The signal lamp is made of metal."
        assert result.belief_history[0].text == "I suspect the lamp is wooden."
        pc = sm.read_session(sid, npc_package, context=context).pc_actor_id
        assert {r.target_id for r in result.relationships} == {pc, "npc.attendant"}
        assert result.goals[0].status == "active"
        assert result.plans[0].goal_id == result.goals[0].record_id
        assert result.plans[0].goal_version == 1
        assert result.plans[0].steps == ("Finish current task.", "Inspect lamp.")
        assert result.runtime.schedule[0].start_tick == 10
        assert result.runtime.current_activity == "Waiting for an inspection."
        assert sm._conn.execute("SELECT count(*) FROM npc_states").fetchone()[0] == 1
        assert sm._conn.execute("SELECT count(*) FROM event_observations").fetchone()[0] == 0
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute("SELECT text FROM world_facts WHERE fact_id='fact.lamp'").fetchone()[0]
            == result.known_facts[0].text
        )
        assert conn.execute("SELECT count(*) FROM npc_record_sources").fetchone()[0] == 7
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_double_npc_and_session_privacy_and_no_world_secret(tmp_path, npc_package, context):
    with StateMachine(tmp_path / "privacy.db") as sm:
        sids = [
            sm.create_session(
                request(npc_package, context, key), npc_package, context=context
            ).session_id
            for key in ("one", "two")
        ]
        for sid in sids:
            enter(sm, sid, npc_package, context, "scene.workshop")
            for npc in ("npc.caretaker", "npc.attendant"):
                grant(sm, sid, npc_package, context, npc)
            a, b = view(sm, sid, npc_package), view(sm, sid, npc_package, "npc.attendant")
            a_memory = next(m for m in a.memories if m.record_id == "memory.backstory")
            b_memory = next(m for m in b.memories if m.record_id == "memory.backstory")
            assert a_memory.record_id == b_memory.record_id
            assert a_memory.text != b_memory.text
            assert [f.fact_id for f in a.known_facts] == ["fact.lamp"]
            assert [f.fact_id for f in b.known_facts] == ["fact.route"]
            assert "buried blue key" not in json.dumps(asdict(a)) + json.dumps(asdict(b))
            with pytest.raises(StateMachineError, match="observer"):
                sm.get_npc_view(
                    sid,
                    "npc.attendant",
                    npc_package,
                    context=NPCContext("agent:npc.caretaker", sid, "npc.caretaker"),
                )
        with pytest.raises(StateMachineError) as caught:
            sm.get_npc_view(
                sids[1],
                "npc.caretaker",
                npc_package,
                context=NPCContext("agent:npc.caretaker", sids[0], "npc.caretaker"),
            )
        assert caught.value.code == "unauthorized"
        assert sm._conn.execute("SELECT count(*) FROM npc_states").fetchone()[0] == 4


@pytest.mark.parametrize(
    "identity", ["missing", "wrong_principal", "wrong_observer", "payload", "owner", "revoked"]
)
def test_npc_view_trusted_authorization(tmp_path, npc_package, context, identity):
    with StateMachine(tmp_path / "auth.db") as sm:
        sid = sm.create_session(
            request(npc_package, context), npc_package, context=context
        ).session_id
        npc_context = NPCContext("agent:npc.caretaker", sid, "npc.caretaker")
        if identity != "missing":
            grant(sm, sid, npc_package, context)
        if identity == "wrong_principal":
            npc_context = replace(npc_context, principal_id="intruder")
        elif identity == "wrong_observer":
            npc_context = replace(npc_context, observer_id="npc.attendant")
        elif identity == "payload":
            npc_context = asdict(npc_context)
        elif identity == "owner":
            npc_context = context
        elif identity == "revoked":
            sm.grant_npc_access(sid, "npc.caretaker", "replacement", npc_package, context=context)
        before = stored_rows(tmp_path / "auth.db")
        with pytest.raises(StateMachineError) as caught:
            sm.get_npc_view(sid, "npc.caretaker", npc_package, context=npc_context)
        assert caught.value.code == "unauthorized"
        assert stored_rows(tmp_path / "auth.db") == before


def test_revisit_movement_reload_and_fresh_process_restore(tmp_path, npc_package, context):
    database = tmp_path / "restore.db"
    with StateMachine(database) as sm:
        created = sm.create_session(request(npc_package, context), npc_package, context=context)
        sid = created.session_id
        assert (
            sm.create_session(request(npc_package, context), npc_package, context=context)
            == created
        )
        enter(sm, sid, npc_package, context, "scene.workshop")
        for npc in ("npc.caretaker", "npc.attendant"):
            grant(sm, sid, npc_package, context, npc)
        before = rows(database)
        snapshot = sm.read_session(sid, npc_package, context=context)
        sm._apply(
            sid,
            npc_package,
            MoveActor("npc.caretaker", "scene.workshop"),
            expected_world_version=snapshot.world_version,
            context=context,
        )
        enter(sm, sid, npc_package, context, "scene.platform")
        enter(sm, sid, npc_package, context, "scene.workshop")
        assert rows(database) == before
        reloaded = load_adventure_package(SOURCE).package
        snapshot = sm.save_session(sid, reloaded, context=context)
        expected = [
            asdict(view(sm, sid, reloaded, npc)) for npc in ("npc.caretaker", "npc.attendant")
        ]
        assert expected[0]["current_scene_id"] == "scene.workshop"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1")
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/_npc_resume_worker.py"),
            str(database),
            str(SOURCE),
            sid,
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    recovered = json.loads(process.stdout)
    assert recovered == json.loads(json.dumps({"snapshot": asdict(snapshot), "views": expected}))
    assert rows(database) == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM npc_states").fetchone()[0] == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_unconfigured_old_fixture_never_invents_profile(tmp_path, package, context):
    with StateMachine(tmp_path / "old.db") as sm:
        sid = sm.create_session(request(package, context), package, context=context).session_id
        grant(sm, sid, package, context)
        result = view(sm, sid, package)
        assert result.configuration == "unconfigured"
        assert result.profile is result.runtime is None
        assert result.memories == result.belief_history == result.goals == ()
        assert result.known_facts == result.relationships == result.plans == ()
        assert result.controller_kind == "scripted_event"


def test_view_does_not_ack_observations_or_grant_command_control(tmp_path, npc_package, context):
    from trpg_state_machine import TypedCommand

    database = tmp_path / "inbox.db"
    with StateMachine(database) as sm:
        owner = replace(context, controlled_npc_ids=())
        sid = sm.create_session(request(npc_package, owner), npc_package, context=owner).session_id
        nc = grant(sm, sid, npc_package, owner)
        for event in sm.read_events(sid, npc_package, context=owner):
            sm.project_event(
                sid, event.event_seq, npc_package, context=owner, judgment=synthetic_notice
            )
        inbox = sm.read_observations(sid, npc_package, context=nc)
        assert inbox
        before = stored_rows(database)
        assert sm.get_npc_view(sid, "npc.caretaker", npc_package, context=nc).memories
        assert sm.read_observations(sid, npc_package, context=nc) == inbox
        assert stored_rows(database) == before
        snapshot = sm.read_session(sid, npc_package, context=owner)
        command = TypedCommand(
            sid,
            "no.control",
            "npc.caretaker",
            "move_actor",
            MoveActor("npc.caretaker", "scene.workshop"),
            snapshot.world_version,
        )
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(command, npc_package, context=owner)
        assert caught.value.code == "unauthorized"
        assert stored_rows(database) == before


@pytest.mark.parametrize(
    "fault",
    [
        "schema",
        "profile_type",
        "extra",
        "fact_other_npc",
        "dm_secret",
        "fact_missing",
        "source_other_npc",
        "source_observation",
        "source_event",
        "source_missing_memory",
        "source_bad_version",
        "source_empty",
        "belief_bool_version",
        "belief_confidence",
        "belief_current",
        "belief_gap",
        "belief_duplicate",
        "belief_status",
        "memory_cycle",
        "goal_reference",
        "relationship_target",
        "schedule_scene",
        "schedule_tick",
        "limit",
        "fact_grant",
    ],
)
def test_invalid_npc_templates_are_structured_and_never_partially_approved(tmp_path, fault):
    data = yaml.safe_load(SOURCE.read_text("utf-8"))
    n = data["actors"][0]["npc_state"]
    b = n["beliefs"][0]
    if fault == "schema":
        n["schema_version"] = "npc-state/future"
    elif fault == "profile_type":
        n["profile"]["personality"] = "patient"
    elif fault == "extra":
        n["profile"]["host_override"] = True
    elif fault == "fact_other_npc":
        n["initial_knowledge_ids"] = ["fact.route"]
    elif fault == "dm_secret":
        n["initial_knowledge_ids"] = ["fact.dm_secret"]
    elif fault == "fact_missing":
        n["initial_knowledge_ids"] = ["fact.missing"]
    elif fault == "source_other_npc":
        b["sources"][0]["reference_id"] = "npc.attendant"
    elif fault in ("source_observation", "source_event"):
        b["sources"][0]["kind"] = fault.removeprefix("source_")
    elif fault == "source_missing_memory":
        b["sources"] = [dict(kind="memory", reference_id="memory.attendant_only", version=1)]
    elif fault == "source_bad_version":
        b["sources"][0]["version"] = 1
    elif fault == "source_empty":
        b["sources"] = []
    elif fault == "belief_bool_version":
        b["version"] = True
    elif fault == "belief_confidence":
        b["confidence_basis_points"] = 10001
    elif fault == "belief_current":
        b["is_current"] = True
    elif fault == "belief_gap":
        n["beliefs"][1]["version"] = 3
    elif fault == "belief_duplicate":
        n["beliefs"].append(copy.deepcopy(b))
    elif fault == "belief_status":
        b["epistemic_status"] = "objective_truth"
    elif fault == "memory_cycle":
        n["memories"][0]["sources"] = [
            dict(kind="memory", reference_id="memory.backstory", version=1)
        ]
    elif fault == "goal_reference":
        n["plans"][0]["goal_id"] = "goal.attendant_only"
    elif fault == "relationship_target":
        n["relationships"][0]["target_id"] = "npc.missing"
    elif fault == "schedule_scene":
        n["runtime"]["schedule"][0]["scene_id"] = "scene.missing"
    elif fault == "schedule_tick":
        n["runtime"]["schedule"][0]["start_tick"] = True
    elif fault == "limit":
        n["profile"]["personality"] = ["trait"] * 17
    elif fault == "fact_grant":
        data["world"]["world_facts"][2]["authorized_npc_ids"] = ["npc.caretaker"]
    source = tmp_path / "invalid.yaml"
    source.write_text(yaml.safe_dump(data), "utf-8")
    result = load_adventure_package(source)
    assert result.status == "invalid"
    assert result.package is None
    assert len(result.errors) == 1
    assert result.errors[0].code in {
        "schema_error",
        "invalid_reference",
        "duplicate_id",
        "unsupported_field",
        "unsupported_schema",
    }


@pytest.mark.parametrize("table", NPC_TABLES)
def test_npc_initialization_sqlite_failure_rolls_back_all_authority(
    tmp_path, npc_package, context, table
):
    database = tmp_path / "rollback.db"
    with StateMachine(database) as sm:
        sm._conn.execute(
            f"CREATE TRIGGER fail_npc BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as caught:
            sm.create_session(request(npc_package, context), npc_package, context=context)
        assert caught.value.code == "storage_error"
        with sqlite3.connect(database) as conn:
            tables = [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            assert all(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0 for t in tables)
        sm._conn.execute("DROP TRIGGER fail_npc")
        sid = sm.create_session(
            request(npc_package, context), npc_package, context=context
        ).session_id
        grant(sm, sid, npc_package, context)
        assert view(sm, sid, npc_package).belief_history


def test_lazy_scene_npc_failure_rolls_back_and_can_retry(tmp_path, npc_package, context):
    database = tmp_path / "scene_failure.db"
    with StateMachine(database) as sm:
        sid = sm.create_session(
            request(npc_package, context), npc_package, context=context
        ).session_id
        before = stored_rows(database)
        sm._conn.execute(
            "CREATE TRIGGER fail_npc BEFORE INSERT ON npc_private_records "
            "WHEN NEW.npc_id='npc.attendant' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as caught:
            enter(sm, sid, npc_package, context, "scene.workshop")
        assert caught.value.code == "storage_error"
        assert stored_rows(database) == before
        sm._conn.execute("DROP TRIGGER fail_npc")
        enter(sm, sid, npc_package, context, "scene.workshop")
        grant(sm, sid, npc_package, context, "npc.attendant")
        assert view(sm, sid, npc_package, "npc.attendant").profile


def test_concurrent_idempotent_creation_initializes_npc_once(tmp_path, npc_package, context):
    database = tmp_path / "concurrent.db"
    barrier = Barrier(4)

    def create(_):
        barrier.wait(timeout=10)
        with StateMachine(database) as sm:
            return sm.create_session(request(npc_package, context), npc_package, context=context)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert all(r == results[0] for r in results)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM npc_states").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM npc_private_records").fetchone()[0] == 7
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_actual_schema_1_to_9_upgrade_preserves_data_and_marks_legacy(
    tmp_path, package, context, version, legacy_python
):
    if version <= 7:
        database, sids = historical_database(tmp_path, version)
    else:
        sha = "a008543" if version == 8 else "a91b0da"
        destination = tmp_path / "historical_source"
        archive = subprocess.run(
            ["git", "archive", "--format=zip", sha, "src", "tests/fixtures/lantern_station.yaml"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        with zipfile.ZipFile(io.BytesIO(archive)) as contents:
            contents.extractall(destination)
        database = tmp_path / "historical.db"
        process = subprocess.run(
            [
                legacy_python if version == 8 else sys.executable,
                str(ROOT / "tests/_legacy_database_worker.py"),
                str(destination),
                str(database),
                str(destination / "tests/fixtures/lantern_station.yaml"),
                "no",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        result = json.loads(process.stdout)
        assert result["schema"] == version
        sids = result["sessions"]
    before = stored_rows(database)
    barrier = Barrier(3)

    def upgrade(_):
        barrier.wait(timeout=10)
        with StateMachine(database) as sm:
            return sm._conn.execute("PRAGMA user_version").fetchone()[0]

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(upgrade, range(3))) == [38] * 3
    assert_preserved(database, before)
    with StateMachine(database) as sm:
        for sid in sids:
            grant(sm, sid, package, context)
            result = view(sm, sid, package)
            assert result.initialization_origin == "legacy"
            assert result.configuration == "unconfigured"
            assert result.profile is result.runtime is result.name is result.controller_kind is None
            assert result.memories == result.belief_history == ()
            assert result.npc_state_version == 1
    upgraded = stored_rows(database)
    for _ in range(2):
        with StateMachine(database):
            pass
        assert stored_rows(database) == upgraded


@pytest.mark.parametrize("interruption", ["sql_error", "process_exit"])
def test_schema10_upgrade_interruption_rolls_back_and_recovers(tmp_path, monkeypatch, interruption):
    from test_migrations import HISTORY

    monkeypatch.setitem(HISTORY, 9, "a91b0da")
    database, _ = historical_database(tmp_path, 9)
    before = stored_rows(database)
    if interruption == "sql_error":
        read_text = Path.read_text

        def broken(path, *args, **kwargs):
            content = read_text(path, *args, **kwargs)
            return (
                content.replace("PRAGMA user_version = 10;", "INVALID MIGRATION;")
                if path.name == "_migration_10.sql"
                else content
            )

        monkeypatch.setattr(Path, "read_text", broken)
        with pytest.raises(StateMachineError) as caught:
            StateMachine(database)
        assert caught.value.code == "storage_error"
        monkeypatch.undo()
    else:
        script = """
import os, sqlite3, sys
from trpg_state_machine import StateMachine
connect = sqlite3.connect
def traced(*args, **kwargs):
    conn = connect(*args, **kwargs)
    def stop(sql):
        if 'CREATE TABLE npc_known_facts' in sql:
            os._exit(23)
    conn.set_trace_callback(stop)
    return conn
sqlite3.connect = traced
StateMachine(sys.argv[1])
"""
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1")
        process = subprocess.run(
            [sys.executable, "-c", script, str(database)],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            check=False,
        )
        assert process.returncode == 23, process.stderr
    assert stored_rows(database) == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    with StateMachine(database):
        pass
    assert_preserved(database, before)


@pytest.mark.parametrize(
    "fault",
    [
        "pc_identity",
        "unknown_identity",
        "cross_npc_source",
        "cross_session_source",
        "dm_fact_grant",
        "invalid_payload",
        "rewrite_history",
        "delete_history",
        "rewrite_source",
    ],
)
def test_sqlite_ownership_and_immutable_history_constraints(tmp_path, npc_package, context, fault):
    database = tmp_path / "constraints.db"
    with StateMachine(database) as sm:
        sid = sm.create_session(
            request(npc_package, context), npc_package, context=context
        ).session_id
        other = sm.create_session(
            request(npc_package, context, "other"), npc_package, context=context
        ).session_id
        enter(sm, sid, npc_package, context, "scene.workshop")
        # These identities exist only in another scope. A source must never resolve there.
        with sm._transaction(write=True):
            for target_sid, npc, rid in (
                (sid, "npc.attendant", "memory.other_npc"),
                (other, "npc.caretaker", "memory.other_session"),
            ):
                sm._conn.execute(
                    "INSERT INTO npc_private_records VALUES (?,?,'memory',?,1,1,?,1)",
                    (
                        target_sid,
                        npc,
                        rid,
                        json.dumps({"text": "Private", "epistemic_status": "told"}),
                    ),
                )
        before = stored_rows(database)
        with pytest.raises(StateMachineError) as caught, sm._transaction(write=True):
            if fault in ("pc_identity", "unknown_identity"):
                npc = "npc.missing"
                if fault == "pc_identity":
                    npc = sm._conn.execute(
                        "SELECT pc_actor_id FROM sessions WHERE session_id=?", (sid,)
                    ).fetchone()[0]
                sm._conn.execute(
                    "INSERT INTO npc_states VALUES (?,?,'npc-state/1',NULL,NULL,'legacy',"
                    "'unconfigured',NULL,NULL,1)",
                    (sid, npc),
                )
            elif fault in ("cross_npc_source", "cross_session_source"):
                rid = "memory.other_npc" if fault == "cross_npc_source" else "memory.other_session"
                sm._conn.execute(
                    "INSERT INTO npc_record_sources VALUES (?,?,'belief','belief.lamp',1,3,"
                    "'memory',?,1,'memory')",
                    (sid, "npc.caretaker", rid),
                )
            elif fault == "dm_fact_grant":
                sm._conn.execute(
                    "INSERT INTO npc_known_facts VALUES (?,?,'fact.dm_secret')",
                    (sid, "npc.caretaker"),
                )
            elif fault == "invalid_payload":
                sm._conn.execute(
                    "INSERT INTO npc_private_records "
                    "VALUES (?,?,'belief','belief.invalid',1,1,'{}',1)",
                    (sid, "npc.caretaker"),
                )
            elif fault == "rewrite_history":
                sm._conn.execute(
                    "UPDATE npc_private_records SET payload_json='{}' WHERE session_id=?", (sid,)
                )
            elif fault == "delete_history":
                sm._conn.execute("DELETE FROM npc_private_records WHERE session_id=?", (sid,))
            else:
                sm._conn.execute(
                    "UPDATE npc_record_sources SET reference_id='npc.attendant' WHERE session_id=?",
                    (sid,),
                )
        assert caught.value.code == "storage_error"
        assert stored_rows(database) == before


def test_deferred_source_failure_rolls_back_complete_session(tmp_path, npc_package, context):
    database = tmp_path / "deferred.db"
    with StateMachine(database) as sm:
        sm._conn.execute(
            "CREATE TRIGGER bad_source AFTER INSERT ON npc_record_sources "
            "WHEN NEW.source_kind='package' BEGIN "
            "INSERT INTO npc_record_sources VALUES (NEW.session_id,NEW.npc_id,NEW.record_kind,"
            "NEW.record_id,NEW.content_version,3,'memory','memory.missing',1,'memory'); END"
        )
        with pytest.raises(StateMachineError) as caught:
            sm.create_session(request(npc_package, context), npc_package, context=context)
        assert caught.value.code == "storage_error"
        with sqlite3.connect(database) as conn:
            tables = [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            assert all(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0 for t in tables)
        sm._conn.execute("DROP TRIGGER bad_source")
        sm.create_session(request(npc_package, context), npc_package, context=context)


def test_npc_view_package_digest_mismatch_has_no_side_effect(tmp_path, npc_package, context):
    database = tmp_path / "digest.db"
    with StateMachine(database) as sm:
        sid = sm.create_session(
            request(npc_package, context), npc_package, context=context
        ).session_id
        grant(sm, sid, npc_package, context)
        before = stored_rows(database)
        with pytest.raises(StateMachineError) as caught:
            view(sm, sid, replace(npc_package, package_digest="sha256:changed"))
        assert caught.value.code == "package_digest_mismatch"
        assert stored_rows(database) == before
