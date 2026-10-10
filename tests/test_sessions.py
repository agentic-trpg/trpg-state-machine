import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier

import pytest
import yaml

from trpg_state_machine import (
    CreateSessionRequest,
    PlayerSelection,
    StateMachine,
    StateMachineError,
    TrustedContext,
    load_adventure_package,
)
from trpg_state_machine._operations import (
    EnterScene,
    MoveActor,
    ObjectState,
    SetObjectState,
    SetSceneFlag,
)

TABLES = (
    "sessions",
    "world_states",
    "scene_instances",
    "objects",
    "actors",
    "characters",
    "rng_states",
    "events",
    "creation_commands",
)


def assert_code(code, action):
    with pytest.raises(StateMachineError) as error:
        action()
    assert error.value.code == code


def test_creation_persists_complete_foundation(tmp_path, package, context, request_for):
    database = tmp_path / "session.db"
    with StateMachine(database) as sm:
        created = sm.create_session(request_for(pc="pc_template.visitor"), package, context=context)
        snapshot = sm.read_session(created.session_id, package, context=context)
        assert snapshot.world.flags == {"flag.station_ready": False}
        assert snapshot.current_scene_id == "scene.platform"
        assert set(snapshot.scenes) == {"scene.platform"}
        assert snapshot.scenes["scene.platform"].visited_count == 1
        assert set(snapshot.actors) == {"npc.caretaker", snapshot.pc_actor_id}
        assert snapshot.player == PlayerSelection("player.alice", "pc_template.visitor")
        assert snapshot.character.current_hp == 11
        assert snapshot.character.max_hp == 12
        assert snapshot.character.resources == {}
        assert snapshot.character.inventory == ()
        assert snapshot.rng.algorithm == "unselected"
        assert snapshot.rng.encoding == "foundation-placeholder/1"
        assert snapshot.rng.position == 0
        assert snapshot.world_version == 0
        assert snapshot.event_seq == 2
        assert set(asdict(created)) == {
            "session_id",
            "adventure_id",
            "package_version",
            "package_digest",
            "ruleset_binding",
            "world_version",
            "event_seq",
        }
        assert sm._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert sm._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert sm._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        row = db.execute("SELECT rng_ref, pc_actor_id, package_digest FROM sessions").fetchone()
        assert row == (snapshot.rng.state_id, snapshot.pc_actor_id, package.package_digest)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_two_sessions_and_detached_snapshots_are_isolated(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "isolation.db") as sm:
        first = sm.create_session(request_for("cmd.first"), package, context=context)
        second = sm.create_session(request_for("cmd.second"), package, context=context)
        assert first.session_id != second.session_id
        changed = sm._apply(
            first.session_id,
            package,
            SetSceneFlag("scene.platform", "flag.lamp_checked", True),
            expected_world_version=0,
            context=context,
        )
        assert changed.world_version == 1
        changed.world.flags["flag.station_ready"] = True
        changed.scenes["scene.platform"].local_flags["flag.lamp_checked"] = False
        changed.scenes["scene.platform"].objects["object.platform_gate"] = "open"
        fresh = sm.read_session(first.session_id, package, context=context)
        untouched = sm.read_session(second.session_id, package, context=context)
        assert fresh.scenes["scene.platform"].local_flags["flag.lamp_checked"] is True
        assert untouched.scenes["scene.platform"].local_flags["flag.lamp_checked"] is False
        assert untouched.world_version == 0
        assert fresh.world.flags == untouched.world.flags == {"flag.station_ready": False}
        assert fresh.scenes["scene.platform"].objects == {"object.platform_gate": "closed"}
        assert fresh.pc_actor_id != untouched.pc_actor_id
        assert fresh.rng.state_id != untouched.rng.state_id
        assert package.scene("scene.platform").flags == (("flag.lamp_checked", False),)


def test_creation_retry_returns_original_result_even_after_changes_and_restart(
    tmp_path, package, context, request_for
):
    path = tmp_path / "retry.db"
    request = request_for()
    with StateMachine(path) as sm:
        original = sm.create_session(request, package, context=context)
        assert sm.create_session(request, package, context=context) == original
        sm._apply(
            original.session_id,
            package,
            EnterScene("scene.workshop"),
            expected_world_version=0,
            context=context,
        )
    with StateMachine(path) as restarted:
        assert restarted.create_session(request, package, context=context) == original
        assert (
            restarted.read_session(original.session_id, package, context=context).world_version == 1
        )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM creation_commands").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM scene_instances").fetchone()[0] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"players": (PlayerSelection("player.alice", "pc_template.visitor"),)},
        {"package_digest": "sha256:different"},
        {"package_version": "different"},
        {"adventure_id": "example.other"},
    ],
)
def test_same_creation_key_different_fingerprint_conflicts(
    tmp_path, package, context, request_for, change
):
    with StateMachine(tmp_path / "conflict.db") as sm:
        request = request_for()
        created = sm.create_session(request, package, context=context)
        before = sm.read_session(created.session_id, package, context=context)
        assert_code(
            "idempotency_conflict",
            lambda: sm.create_session(replace(request, **change), package, context=context),
        )
        assert sm.read_session(created.session_id, package, context=context) == before
        assert sm._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_command_keys_are_scoped_by_trusted_principal(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "principals.db") as sm:
        request = request_for()
        first = sm.create_session(request, package, context=context)
        other_context = TrustedContext("authenticated:bob", "player.bob")
        other_request = replace(
            request, players=(PlayerSelection("player.bob", "pc_template.observer"),)
        )
        second = sm.create_session(other_request, package, context=other_context)
        assert first.session_id != second.session_id
        assert_code(
            "unauthorized",
            lambda: sm.read_session(first.session_id, package, context=other_context),
        )
        assert_code(
            "unauthorized", lambda: sm.create_session(request, package, context=other_context)
        )


def test_concurrent_creation_claim_is_durable_and_unique(tmp_path, package, context, request_for):
    database = tmp_path / "concurrent.db"
    with StateMachine(database):
        pass
    barrier = Barrier(4)
    request = request_for()

    def create(_):
        with StateMachine(database) as sm:
            barrier.wait(timeout=10)
            return sm.create_session(request, package, context=context)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert results == [results[0]] * 4
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM creation_commands").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


@pytest.mark.parametrize("table", ["characters", "rng_states", "events", "creation_commands"])
def test_mid_creation_storage_failure_rolls_back_every_table(
    tmp_path, package, context, request_for, table
):
    database = tmp_path / "rollback.db"
    with StateMachine(database) as sm:
        with sqlite3.connect(database) as db:
            db.execute(
                f"CREATE TRIGGER injected_failure BEFORE INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        assert_code(
            "storage_error", lambda: sm.create_session(request_for(), package, context=context)
        )
        assert not sm._conn.in_transaction
    with sqlite3.connect(database) as db:
        for name in TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0, name
        db.execute("DROP TRIGGER injected_failure")
    with StateMachine(database) as sm:
        created = sm.create_session(request_for(), package, context=context)
        assert sm.resume_session(created.session_id, package, context=context).world_version == 0


def test_deferred_commit_failure_rolls_back_creation(tmp_path, package, context, request_for):
    path = tmp_path / "commit_failure.db"
    with StateMachine(path) as sm:
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TRIGGER break_deferred_fk AFTER INSERT ON creation_commands BEGIN
                    UPDATE sessions SET current_scene_id = 'scene.missing'
                    WHERE session_id = NEW.session_id;
                END;
            """)
        assert_code(
            "storage_error", lambda: sm.create_session(request_for(), package, context=context)
        )
        assert not sm._conn.in_transaction
    with sqlite3.connect(path) as db:
        for table in TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"players": ()}, "invalid_player_setup"),
        (
            {"players": (PlayerSelection("player.alice", "pc_template.observer"),) * 2},
            "invalid_player_setup",
        ),
        ({"players": (PlayerSelection("player.mallory", "pc_template.observer"),)}, "unauthorized"),
        (
            {"players": (PlayerSelection("player.alice", "pc_template.missing"),)},
            "unknown_pc_template",
        ),
        ({"package_digest": "sha256:wrong"}, "package_digest_mismatch"),
        ({"package_version": "other"}, "package_identity_mismatch"),
        ({"command_id": ""}, "invalid_request"),
    ],
)
def test_invalid_creation_leaves_no_records(tmp_path, package, context, request_for, change, code):
    with StateMachine(tmp_path / "invalid.db") as sm:
        assert_code(
            code,
            lambda: sm.create_session(replace(request_for(), **change), package, context=context),
        )
        for table in TABLES:
            assert sm._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_untrusted_payload_cannot_supply_context(tmp_path, package, request_for):
    with StateMachine(tmp_path / "auth.db") as sm:
        assert_code(
            "invalid_context",
            lambda: sm.create_session(request_for(), package, context={"principal_id": "forged"}),
        )
    with pytest.raises(TypeError):
        CreateSessionRequest(**asdict(request_for()), principal_id="forged")


def test_scene_revisit_preserves_flags_objects_and_unique_actor_location(
    tmp_path, package, context, request_for
):
    with StateMachine(tmp_path / "scenes.db") as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
        state = sm._apply(
            sid, package, EnterScene("scene.workshop"), expected_world_version=0, context=context
        )
        assert state.scenes["scene.workshop"].visited_count == 1
        assert state.actors["npc.caretaker"].current_scene_id == "scene.platform"
        state = sm._apply(
            sid,
            package,
            SetObjectState("scene.workshop", "object.workshop_door", ObjectState.OPEN),
            expected_world_version=state.world_version,
            context=context,
        )
        state = sm._apply(
            sid,
            package,
            SetSceneFlag("scene.workshop", "flag.workshop_checked", True),
            expected_world_version=state.world_version,
            context=context,
        )
        state = sm._apply(
            sid,
            package,
            MoveActor("npc.caretaker", "scene.workshop"),
            expected_world_version=state.world_version,
            context=context,
        )
        state = sm._apply(
            sid,
            package,
            EnterScene("scene.platform"),
            expected_world_version=state.world_version,
            context=context,
        )
        assert state.actors["npc.caretaker"].current_scene_id == "scene.workshop"
        state = sm._apply(
            sid,
            package,
            EnterScene("scene.workshop"),
            expected_world_version=state.world_version,
            context=context,
        )
        assert state.scenes["scene.workshop"].visited_count == 2
        assert state.scenes["scene.workshop"].local_flags == {"flag.workshop_checked": True}
        assert state.scenes["scene.workshop"].objects == {"object.workshop_door": "open"}
        assert len(state.actors) == 3
        assert state.world_version == 6
        assert state.event_seq == 11


def test_noop_does_not_advance_versions_or_reenter_scene(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "noop.db") as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
        before = sm.read_session(sid, package, context=context)
        for operation in (
            EnterScene("scene.platform"),
            SetSceneFlag("scene.platform", "flag.lamp_checked", False),
            SetObjectState("scene.platform", "object.platform_gate", ObjectState.CLOSED),
            MoveActor("npc.caretaker", "scene.platform"),
        ):
            assert (
                sm._apply(sid, package, operation, expected_world_version=0, context=context)
                == before
            )
        assert sm.save_session(sid, package, context=context) == before


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        ({"path": "/hp", "value": 900}, "unsupported_operation"),
        (SetSceneFlag("scene.workshop", "flag.workshop_checked", True), "invalid_target"),
        (SetSceneFlag("scene.platform", "flag.undeclared", True), "invalid_target"),
        (SetSceneFlag("scene.platform", "flag.lamp_checked", 1), "invalid_operation"),
        (SetObjectState("scene.platform", "object.missing", ObjectState.OPEN), "invalid_target"),
        (SetObjectState("scene.platform", "object.platform_gate", "open"), "invalid_operation"),
        (MoveActor("npc.missing", "scene.platform"), "invalid_target"),
        (MoveActor("npc.caretaker", "scene.workshop"), "invalid_target"),
        (EnterScene("scene.missing"), "unknown_scene"),
    ],
)
def test_internal_allowlist_rejects_without_state_change(
    tmp_path, package, context, request_for, operation, code
):
    with StateMachine(tmp_path / "reject.db") as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
        before = sm.read_session(sid, package, context=context)
        assert_code(
            code,
            lambda: sm._apply(sid, package, operation, expected_world_version=0, context=context),
        )
        assert sm.read_session(sid, package, context=context) == before


def test_stale_version_and_wrong_principal_cannot_write(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "version.db") as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
        operation = SetSceneFlag("scene.platform", "flag.lamp_checked", True)
        after = sm._apply(sid, package, operation, expected_world_version=0, context=context)
        assert_code(
            "version_conflict",
            lambda: sm._apply(sid, package, operation, expected_world_version=0, context=context),
        )
        assert_code(
            "invalid_operation",
            lambda: sm._apply(
                sid, package, operation, expected_world_version=True, context=context
            ),
        )
        assert_code(
            "unauthorized",
            lambda: sm._apply(
                sid,
                package,
                operation,
                expected_world_version=1,
                context=TrustedContext("forged", context.player_id),
            ),
        )
        assert sm.read_session(sid, package, context=context) == after


def test_mutation_and_event_failure_roll_back_together(tmp_path, package, context, request_for):
    database = tmp_path / "mutation_failure.db"
    with StateMachine(database) as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
        before = sm.read_session(sid, package, context=context)
        with sqlite3.connect(database) as db:
            db.execute(
                "CREATE TRIGGER fail_event BEFORE INSERT ON events "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        assert_code(
            "storage_error",
            lambda: sm._apply(
                sid,
                package,
                SetObjectState("scene.platform", "object.platform_gate", ObjectState.OPEN),
                expected_world_version=0,
                context=context,
            ),
        )
        assert sm.read_session(sid, package, context=context) == before
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT state FROM objects").fetchone()[0] == "closed"
            assert db.execute("SELECT event_seq, world_version FROM sessions").fetchone() == (2, 0)


def test_digest_mismatch_rejects_recovery_without_migration(
    tmp_path, package, package_data, context, request_for
):
    database = tmp_path / "digest.db"
    with StateMachine(database) as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
    package_data["scenes"][0]["initial_state"]["local_flags"]["flag.lamp_checked"] = True
    changed_path = tmp_path / "changed.yaml"
    changed_path.write_text(yaml.safe_dump(package_data), "utf-8")
    changed = load_adventure_package(changed_path).package
    with StateMachine(database) as sm:
        assert_code(
            "package_digest_mismatch", lambda: sm.resume_session(sid, changed, context=context)
        )
        assert sm.resume_session(sid, package, context=context).world_version == 0
    with sqlite3.connect(database) as db:
        assert (
            db.execute("SELECT package_digest FROM sessions").fetchone()[0]
            == package.package_digest
        )
        assert json.loads(db.execute("SELECT flags_json FROM scene_instances").fetchone()[0]) == {
            "flag.lamp_checked": False
        }


def test_fixed_ruleset_and_identity_are_verified_on_resume(tmp_path, package, context, request_for):
    database = tmp_path / "pin.db"
    with StateMachine(database) as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
        assert_code(
            "package_identity_mismatch",
            lambda: sm.resume_session(
                sid, replace(package, package_version="future"), context=context
            ),
        )
        with sqlite3.connect(database) as db:
            db.execute("UPDATE sessions SET data_revision = 'tampered'")
        assert_code("ruleset_mismatch", lambda: sm.resume_session(sid, package, context=context))


def test_connection_close_unknown_session_and_unsupported_storage(tmp_path, package, context):
    sm = StateMachine(tmp_path / "closed.db")
    assert_code(
        "session_not_found", lambda: sm.read_session("session.missing", package, context=context)
    )
    sm.close()
    sm.close()
    assert_code("closed", lambda: sm.read_session("session.missing", package, context=context))
    assert_code("unsupported_storage", lambda: StateMachine(":memory:"))
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 99")
    assert_code("unsupported_storage_schema", lambda: StateMachine(path))


def test_storage_open_errors_are_structured(tmp_path):
    assert_code("storage_error", lambda: StateMachine(tmp_path / "missing" / "session.db"))


def test_invalid_package_cannot_initialize_session(tmp_path, request_for, context):
    invalid_source = tmp_path / "invalid.yaml"
    invalid_source.write_text("manifest: {}", "utf-8")
    rejected = load_adventure_package(invalid_source)
    assert rejected.status == "invalid"
    with StateMachine(tmp_path / "invalid_package.db") as sm:
        assert_code(
            "invalid_request",
            lambda: sm.create_session(request_for(), rejected.package, context=context),
        )
        assert sm._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_retry_cannot_substitute_package_content(
    tmp_path, package, package_data, request_for, context
):
    with StateMachine(tmp_path / "substitution.db") as sm:
        original = sm.create_session(request_for(), package, context=context)
        package_data["manifest"]["title"] = "Changed approved content"
        source = tmp_path / "substitute.yaml"
        source.write_text(yaml.safe_dump(package_data), "utf-8")
        substitute = load_adventure_package(source).package
        assert_code(
            "package_digest_mismatch",
            lambda: sm.create_session(request_for(), substitute, context=context),
        )
        assert sm.create_session(request_for(), package, context=context) == original


def test_invalid_typed_selection_is_rejected_before_fingerprinting(
    tmp_path, package, request_for, context
):
    malformed = replace(request_for(), players=(PlayerSelection(context.player_id, object()),))
    with StateMachine(tmp_path / "malformed.db") as sm:
        assert_code(
            "invalid_player_setup", lambda: sm.create_session(malformed, package, context=context)
        )


def test_failed_new_scene_initialization_leaves_no_scene_or_actor(
    tmp_path, package, request_for, context
):
    database = tmp_path / "scene_failure.db"
    with StateMachine(database) as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
        before = sm.read_session(sid, package, context=context)
        with sqlite3.connect(database) as db:
            db.execute(
                "CREATE TRIGGER fail_new_actor BEFORE INSERT ON actors "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        assert_code(
            "storage_error",
            lambda: sm._apply(
                sid,
                package,
                EnterScene("scene.workshop"),
                expected_world_version=0,
                context=context,
            ),
        )
        assert sm.read_session(sid, package, context=context) == before
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT COUNT(*) FROM scene_instances").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 1


def test_concurrent_different_fingerprints_have_one_winner(tmp_path, package, context, request_for):
    database = tmp_path / "concurrent_conflict.db"
    with StateMachine(database):
        pass
    barrier = Barrier(2)

    def create(pc):
        with StateMachine(database) as sm:
            barrier.wait(timeout=10)
            try:
                return sm.create_session(request_for(pc=pc), package, context=context)
            except StateMachineError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["pc_template.observer", "pc_template.visitor"]))
    assert results.count("idempotency_conflict") == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 1
