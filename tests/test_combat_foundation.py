import json
import sqlite3
from dataclasses import replace

import pytest
import yaml
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd_support import create, initialize, loader, make_package
from dnd_support import package_data as dnd_package_data
from test_migrations import assert_preserved, historical_database, stored_rows

from trpg_state_machine import StateMachine, StateMachineError, load_adventure_package


def test_complete_snapshot_rng_isolation_and_restart(tmp_path, package_data, context):
    assets = loader()
    package = make_package(package_data, assets)
    db = tmp_path / "combat.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        other = create(sm, package, context, "create.other")
        receipt = initialize(sm, sid, package, context)
        assert receipt.status == "committed"
        assert receipt.world_version == 1
        assert (
            initialize(sm, sid, package, context, "init.other").error_code
            == "combat_already_initialized"
        )
        snapshot = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        assert CombatSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
        assert "rng" not in snapshot.model_dump_json()
        assert rng.state.restore().getstate()[0] == 3
        assert sm._conn.execute("SELECT count(*) FROM combats").fetchone()[0] == 1
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM mechanical_actors WHERE session_id=?", (other,)
            ).fetchone()[0]
            == 0
        )
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        assert snapshot.character_states[0].entity_id == pc
        assert snapshot.character_states[0].hp_current == 8
        with pytest.raises(StateMachineError, match="active-combat"):
            sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=assets) as restarted:
        assert restarted.read_combat_snapshot(sid, package, context=context) == snapshot
        assert restarted.read_rng_context(sid, package, context=context) == rng
        with pytest.raises(StateMachineError, match="active-combat"):
            restarted.resume_session(sid, package, context=context)
    with sqlite3.connect(db) as conn:
        data = json.loads(
            conn.execute(
                "SELECT state_json FROM mechanical_actors WHERE actor_id=?", (pc,)
            ).fetchone()[0]
        )
        assert not {"hp_current", "hp_max", "carried_item_slugs"} & data.keys()
        assert "event_log" not in json.loads(
            conn.execute("SELECT state_json FROM combats").fetchone()[0]
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "section,field",
    [
        ("actor", "action_available"),
        ("actor", "senses"),
        ("actor", "spell_slots"),
        ("combat", "movement_ledgers"),
        ("combat", "pending_reactions"),
        ("scene", "grid"),
        ("snapshot", "effect_states"),
    ],
)
def test_missing_mechanical_dependencies_reject_package(tmp_path, package_data, section, field):
    data = dnd_package_data(package_data, loader())
    s = data["rule_bindings"]["combat_foundation"]["snapshot"]
    target = {
        "actor": s["character_states"][0],
        "combat": s["combat_state"],
        "scene": s["scene_state"],
        "snapshot": s,
    }[section]
    del target[field]
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = load_adventure_package(path)
    assert result.status == "invalid" and result.package is None
    assert result.errors[0].code == "invalid_combat_foundation"


def test_binding_errors_do_not_create_session(tmp_path, package_data, context):
    assets = loader()
    package = make_package(package_data, assets)
    bad = replace(
        package,
        ruleset_binding=replace(package.ruleset_binding, data_revision="sha256:" + "0" * 64),
    )
    with StateMachine(tmp_path / "binding.db", dnd_loader=assets) as sm:
        with pytest.raises(StateMachineError, match="data/configuration"):
            create(sm, bad, context)
        assert sm._conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        sid = create(sm, package, context)
        initialize(sm, sid, package, context)
        with pytest.raises(StateMachineError) as exc:
            sm.read_combat_snapshot(sid, bad, context=context)
        assert exc.value.code == "ruleset_mismatch"


def test_initialize_rollback_and_retry(tmp_path, package_data, context):
    assets = loader()
    package = make_package(package_data, assets)
    with StateMachine(tmp_path / "rollback.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        before = sm.read_session(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_combat BEFORE INSERT ON combats "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError, match="SQLite transaction failed"):
            initialize(sm, sid, package, context)
        assert sm.read_session(sid, package, context=context) == before
        assert sm._conn.execute("SELECT count(*) FROM mechanical_actors").fetchone()[0] == 0
        assert sm._conn.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0
        sm._conn.execute("DROP TRIGGER fail_combat")
        assert initialize(sm, sid, package, context).status == "committed"


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6])
def test_combat_migration_preserves_real_old_database(tmp_path, package, context, version):
    db, sessions = historical_database(tmp_path, version)
    before = stored_rows(db)
    with StateMachine(db) as sm:
        for sid in sessions:
            assert sm.resume_session(sid, package, context=context).session_id == sid
        assert sm._conn.execute("SELECT count(*) FROM combats").fetchone()[0] == 0
    assert_preserved(db, before)


def test_authored_foundation_uses_public_package_loader(tmp_path, package_data):
    data = dnd_package_data(package_data, loader())
    path = tmp_path / "original.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_adventure_package(path).status == "valid"
