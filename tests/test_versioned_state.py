"""V2 facts before combat, unique SQL ownership, isolation and migration rollback."""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml
from dnd_support import create, initialize
from modern_support import modern_data, modern_loader, modern_package
from test_dnd_ledger_scope import stored_state

from trpg_state_machine import StateMachine, StateMachineError, load_adventure_package
from trpg_state_machine.package import _validate


def test_native_facts_before_combat_and_initialization_preserve_rng(
    tmp_path, package_data, context
):
    assets = modern_loader()
    package = modern_package(package_data, assets)
    database = tmp_path / "native.db"
    with StateMachine(database, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        other = create(sm, package, context, "create.other")
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        assert before.snapshot_schema_version == "engine-snapshot/2"
        assert before.snapshot_kind == "non_combat"
        assert "combat_state" not in before.model_dump()
        assert sm._conn.execute("SELECT count(*) FROM combats").fetchone()[0] == 0
        assert sm.read_session(sid, package, context=context).character.xp == 10
        assert before.character_states[1].weapon_in_hands == "rapier"
        rng = sm.read_rng_context(sid, package, context=context)
        assert initialize(sm, sid, package, context).status == "committed"
        combat = sm.read_combat_snapshot(sid, package, context=context)
        assert combat.character_states == tuple(
            sorted(before.character_states, key=lambda a: a.entity_type != "Character")
        )
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_noncombat_snapshot(other, package, context=context).world_version == 0
        with sqlite3.connect(database) as db:
            for (encoded,) in db.execute("SELECT state_json FROM mechanical_actors"):
                assert (
                    not {"hp_current", "hp_max", "carried_item_slugs", "xp"}
                    & json.loads(encoded).keys()
                )
        with pytest.raises(StateMachineError) as caught:
            sm.read_noncombat_snapshot(sid, package, context=context)
        assert caught.value.code == "combat_unrestorable"
    with StateMachine(database, dnd_loader=assets) as reopened:
        assert reopened.read_noncombat_snapshot(other, package, context=context).character_states
        assert reopened.read_combat_snapshot(sid, package, context=context) == combat


@pytest.mark.parametrize(
    "fault",
    [
        "missing_ability",
        "missing_grip",
        "grip",
        "hand",
        "hp",
        "xp",
        "reference",
        "initial_mismatch",
    ],
)
def test_invalid_complete_native_facts_reject_package(tmp_path, package_data, fault):
    data = modern_data(package_data, modern_loader())
    noncombat = data["rule_bindings"]["noncombat_foundation"]
    pc = noncombat["snapshot"]["character_states"][0]
    if fault == "missing_ability":
        del pc["strength"]
    elif fault == "missing_grip":
        del pc["weapon_grip"]
    elif fault == "grip":
        pc["weapon_grip"] = "none"
    elif fault == "hand":
        pc.update(weapon_grip="two_handed", other_hand_occupied=True)
    elif fault == "hp":
        pc["hp_current"] = pc["hp_max"] + 1
    elif fault == "xp":
        noncombat["xp"][pc["entity_id"]] = -1
    elif fault == "reference":
        noncombat["additional_scenes"][0]["scene_id"] = "scene.absent"
    else:
        pc["strength"] += 1
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = load_adventure_package(path)
    assert result.status == "invalid" and result.package is None and result.errors


def test_noncombat_creation_sql_fault_rolls_back_every_table(tmp_path, package_data, context):
    assets = modern_loader()
    package = modern_package(package_data, assets)
    database = tmp_path / "create.db"
    with StateMachine(database, dnd_loader=assets) as sm:
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_native BEFORE INSERT ON mechanical_actors "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        before = stored_state(database)
        with pytest.raises(StateMachineError) as caught:
            create(sm, package, context)
        assert caught.value.code == "storage_error" and stored_state(database) == before
        sm._conn.execute("DROP TRIGGER fail_native")
        sid = create(sm, package, context)
        assert create(sm, package, context) == sid


def test_schema9_interrupt_rolls_back_and_restart_completes(tmp_path, monkeypatch, legacy_python):
    database = tmp_path / "schema8.db"
    subprocess.run(
        [
            legacy_python,
            "-c",
            "import sys; from trpg_state_machine import StateMachine; "
            "StateMachine(sys.argv[1]).close()",
            str(database),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    original = stored_state(database)
    real = Path.read_text

    def fault(path, *args, **kwargs):
        text = real(path, *args, **kwargs)
        return (
            text.replace("PRAGMA user_version = 9;", "INVALID MIGRATION;")
            if path.name == "_migration_9.sql"
            else text
        )

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fault)
        with pytest.raises(StateMachineError):
            StateMachine(database)
    assert stored_state(database) == original
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert "xp" not in {r[1] for r in db.execute("PRAGMA table_info(characters)")}
    for _ in range(2):
        with StateMachine(database) as sm:
            assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 14


def test_check_only_package_has_no_encounter_dependency(tmp_path, package_data, context):
    assets = modern_loader()
    data = modern_data(package_data, assets)
    del data["rule_bindings"]["combat_foundation"]
    package = _validate(data)
    with StateMachine(tmp_path / "checks.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert sm.read_noncombat_snapshot(sid, package, context=context).character_states
        before = stored_state(tmp_path / "checks.db")
        with pytest.raises(StateMachineError) as caught:
            initialize(sm, sid, package, context)
        assert caught.value.code == "unsupported_operation"
        assert stored_state(tmp_path / "checks.db") == before


def test_authorized_noncombat_reads_still_require_owner(tmp_path, package_data, context):
    from dataclasses import replace

    assets = modern_loader()
    package = modern_package(package_data, assets)
    with StateMachine(tmp_path / "read.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        before = stored_state(tmp_path / "read.db")
        with pytest.raises(StateMachineError) as caught:
            sm.read_noncombat_snapshot(
                sid, package, context=replace(context, principal_id="outsider")
            )
        assert caught.value.code == "unauthorized"
        assert stored_state(tmp_path / "read.db") == before
