"""RF4 real pinned evaluator, persisted current facts and explicit old-format policy."""

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import zipfile
from copy import deepcopy
from dataclasses import replace
from importlib.metadata import distribution
from pathlib import Path

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import create, initialize
from modern_support import modern_data, modern_loader
from test_dnd_ledger_scope import stored_state
from test_dnd_runtime import attack as lantern_attack
from test_migrations import assert_original_rows_equal, stored_rows
from test_real_checks import check_command

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.package import _validate
from trpg_state_machine.rules_migration import migrate_package_16

ROOT = Path(__file__).resolve().parents[1]
PIN = "d596161060555630019163ab301e1d25fd816549"
BASELINE = "5ff7a807d5b29cceb4311dcb2fe5b1182ead6bd1"


def attack(sm, sid, package, context):
    command = lantern_attack(sm, sid, package, context)
    return replace(command, payload=command.payload.model_copy(update={"weapon_id": "rapier"}))


def setup(tmp_path, base, context, *, active=True):
    assets = modern_loader()
    data = modern_data(base, assets, npc_hp=50)
    package = _validate(data)
    database = tmp_path / "rf4.db"
    sm = StateMachine(database, dnd_loader=assets)
    sid = create(sm, package, context)
    if active:
        assert initialize(sm, sid, package, context).status == "committed"
    return sm, sid, package, assets, database


def test_installed_remote_engine_and_srd_match_fixed_full_sha():
    for name in ("dnd5e-engine", "dnd5e-srd-data"):
        source = json.loads(distribution(name).read_text("direct_url.json"))
        assert source["vcs_info"]["commit_id"] == PIN
        assert source["url"] == "https://github.com/agentic-trpg/trpg-rules-engine.git"


def test_submit_occ_and_availability_do_not_select_audit_history(tmp_path, package_data, context):
    sm, sid, package, _, _ = setup(tmp_path, package_data, context)
    with sm:
        sql = []
        sm._conn.set_trace_callback(sql.append)
        command = attack(sm, sid, package, context)
        available = sm.query_action_availability(
            sid, command.actor_id, command.payload, package, context=context
        )
        assert available.schema_version == "engine-availability/13"
        assert available.status == "available"
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(command, package, context=context) == receipt
        sm._conn.set_trace_callback(None)
        selections = [
            " ".join(s.lower().split()) for s in sql if s.lstrip().upper().startswith("SELECT")
        ]
        audit_reads = [s for s in selections if "from events " in s or "join events " in s]
        # Queue persistence may read each newly committed event by its primary key.
        # The evaluator/OCC must never scan the historical prefix.
        for statement in audit_reads:
            key = re.search(r"and event_seq=(\d+)$", statement)
            assert key is not None and int(key[1]) in receipt.event_seqs
        snapshot = sm.read_combat_snapshot(sid, package, context=context)
        assert "event_log" not in snapshot.combat_state.model_dump()
        history = sm.read_events(sid, package, context=context)
        assert [e.event_seq for e in history] == list(range(1, history[-1].event_seq + 1))
        assert tuple(e.event_seq for e in history if e.source_command_id == command.command_id) == (
            receipt.event_seqs
        )


@pytest.mark.parametrize("fault", ["missing", "unknown_owner", "unknown_field", "core_resource"])
def test_unknown_component_facts_never_fall_back_to_package(tmp_path, package_data, context, fault):
    sm, sid, package, _, db = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        if fault == "missing":
            sm._conn.execute("DELETE FROM mechanical_components WHERE session_id=?", (sid,))
        elif fault == "core_resource":
            sm._conn.execute(
                "UPDATE characters SET resources_json=? WHERE session_id=? AND actor_id=?",
                ('{"unknown":1}', sid, command.actor_id),
            )
        else:
            field = "owner_id" if fault == "unknown_owner" else "unknown_field"
            sm._conn.execute(
                "UPDATE mechanical_components SET inventory_json=json_set(inventory_json,?,?) "
                "WHERE session_id=?",
                ("$.entries[0]." + field, "absent.actor", sid),
            )
        before = stored_state(db)
        calls = []
        real = sm._rules._evaluate_dnd

        async def capture(request):
            calls.append(request)
            return await real(request)

        sm._rules._evaluate_dnd = capture
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == (
            "unsupported_mechanical_version" if fault == "missing" else "invalid_mechanical_state"
        )
        assert not calls and stored_state(db) == before


def test_component_dependency_change_conflicts_without_committing_real_result(
    tmp_path, package_data, context
):
    sm, sid, package, _, db = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        before = stored_state(db)
        real = sm._rules._evaluate_dnd

        async def concurrent_update(request):
            assert not sm._conn.in_transaction
            result = await real(request)
            assert result.status == "accepted"
            with sqlite3.connect(db) as other:
                other.execute(
                    "UPDATE mechanical_components SET inventory_json="
                    "json_set(inventory_json,'$.entries[0].quantity',2) WHERE session_id=?",
                    (sid,),
                )
            return result

        sm._rules._evaluate_dnd = concurrent_update
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "conflict" and not receipt.event_seqs
        after = stored_state(db)
        assert {
            k: v
            for k, v in before.items()
            if k not in ("mechanical_components", "command_receipts")
        } == {
            k: v for k, v in after.items() if k not in ("mechanical_components", "command_receipts")
        }
        assert sm.submit_command(command, package, context=context) == receipt


def test_actual_positions_are_read_and_unknown_position_is_never_reset(
    tmp_path, package_data, context
):
    sm, sid, package, _, db = setup(tmp_path, package_data, context)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        assert "actor_zone" not in json.loads(
            sm._conn.execute(
                "SELECT state_json FROM combats WHERE session_id=?", (sid,)
            ).fetchone()[0]
        )
        sm._conn.execute(
            "UPDATE actors SET cell='0,1' WHERE session_id=? AND actor_id=?", (sid, pc)
        )
        assert (
            sm.read_combat_snapshot(sid, package, context=context).combat_state.actor_zone[pc]
            == "0,1"
        )
        sm._conn.execute("UPDATE actors SET cell=NULL WHERE session_id=? AND actor_id=?", (sid, pc))
        before = stored_state(db)
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(attack(sm, sid, package, context), package, context=context)
        assert error.value.code == "invalid_spatial_state" and stored_state(db) == before


@pytest.mark.parametrize("intent", ["dash", "dodge", "disengage", "pass"])
def test_new_engine_capabilities_remain_outside_existing_sm_admission(
    tmp_path, package_data, context, intent
):
    sm, sid, package, _, _ = setup(tmp_path, package_data, context)
    with sm:
        command = replace(
            attack(sm, sid, package, context), payload=CombatIntentPayload(intent_type=intent)
        )
        answer = sm.query_action_availability(
            sid, command.actor_id, command.payload, package, context=context
        )
        assert answer.status == "unknown" and answer.reason.code == "intent.capability"
        before = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "rejected" and receipt.error_code == "intent.capability"
        assert sm.read_rng_context(sid, package, context=context) == before


def predecessor(data):
    old = deepcopy(data)
    old["manifest"]["ruleset_binding"]["evaluator_version"] = "dnd5e-evaluation/35"
    for foundation in old["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/16"
        if snapshot["snapshot_kind"] == "combat":
            snapshot["combat_state"].update(
                event_log=[], legendary_resistance_applied_event_indices=[]
            )
    return old


def test_explicit_complete_predecessor_package_migration_and_real_new_session(
    tmp_path, package_data, context
):
    assets = modern_loader()
    old = predecessor(modern_data(package_data, assets, npc_hp=50))
    original = deepcopy(old)
    new = migrate_package_16(old, loader=assets, package_version="rf4.migrated")
    assert old == original
    assert new["manifest"]["ruleset_binding"]["evaluator_version"] == "dnd5e-evaluation/36"
    assert new["manifest"]["package_version"] != old["manifest"]["package_version"]
    for kind, foundation in new["rule_bindings"].items():
        assert foundation["rng_state"] == old["rule_bindings"][kind]["rng_state"]
        assert foundation["snapshot"]["snapshot_schema_version"] == "engine-snapshot/17"
    package = _validate(new)
    context = replace(context, can_adjudicate_checks=True)
    with StateMachine(tmp_path / "converted.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert (
            sm.submit_command(
                check_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        assert initialize(sm, sid, package, context).status == "committed"
        assert (
            sm.submit_command(attack(sm, sid, package, context), package, context=context).status
            == "committed"
        )


@pytest.mark.parametrize(
    "fault", ["schema2", "missing_fact", "contradictory_ended", "binding", "same_version"]
)
def test_unprovable_package_conversion_refuses_without_mutating_source(package_data, fault):
    assets = modern_loader()
    old = predecessor(modern_data(package_data, assets))
    snapshot = old["rule_bindings"]["combat_foundation"]["snapshot"]
    version = "rf4.new"
    if fault == "schema2":
        snapshot["snapshot_schema_version"] = "engine-snapshot/2"
    elif fault == "missing_fact":
        del snapshot["inventory_state"]
    elif fault == "contradictory_ended":
        snapshot["combat_state"]["ended"] = True
    elif fault == "binding":
        old["manifest"]["ruleset_binding"]["data_revision"] = "sha256:" + "0" * 64
    else:
        version = old["manifest"]["package_version"]
    before = deepcopy(old)
    with pytest.raises(StateMachineError) as error:
        migrate_package_16(old, loader=assets, package_version=version)
    assert error.value.code == "unsupported_rule_migration" and old == before


def test_initial_context_components_must_agree(package_data):
    assets = modern_loader()
    data = modern_data(package_data, assets)
    snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
    snapshot["inventory_state"]["entries"][0]["quantity"] = 2
    with pytest.raises(StateMachineError) as error:
        _validate(data)
    assert error.value.code == "invalid_reference"


def schema22_database(tmp_path):
    source = tmp_path / "baseline22"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", BASELINE, "src", "tests/fixtures/lantern_station.yaml"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        files.extractall(source)
    database = tmp_path / "schema22.db"
    worker = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/_legacy_database_worker.py"),
            str(source),
            str(database),
            str(source / "tests/fixtures/lantern_station.yaml"),
            "no",
        ],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(source / "src")),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(worker.stdout)["schema"] == 22
    return database


@pytest.mark.parametrize(
    "stage",
    [
        "DROP COLUMN mechanical_schema_version",
        "CREATE TABLE mechanical_components",
        "PRAGMA user_version = 23",
    ],
)
def test_actual_schema22_upgrade_failure_is_atomic_and_retry_preserves_every_old_row(
    tmp_path, monkeypatch, stage
):
    db = schema22_database(tmp_path)
    before = stored_rows(db)
    connect = sqlite3.connect

    class Failing(sqlite3.Connection):
        def execute(self, sql, *args):
            result = super().execute(sql, *args)
            if stage in sql:
                raise sqlite3.OperationalError("injected RF4 migration failure")
            return result

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, factory=Failing, **k))
    with pytest.raises(StateMachineError) as error:
        StateMachine(db)
    assert error.value.code == "storage_error"
    monkeypatch.undo()
    assert stored_rows(db) == before
    with StateMachine(db) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 31
        assert not sm._conn.execute("SELECT * FROM mechanical_components").fetchall()
    assert_original_rows_equal(before, stored_rows(db))


def test_combat_only_initialization_uses_existing_hp(tmp_path, package_data, context):
    from dnd_support import loader, make_package

    assets = loader()
    package = make_package(package_data, assets)
    with StateMachine(tmp_path / "initial-hp.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._store.transaction(write=True):
            sm._characters.set_hp(sid, pc, 3)
        assert initialize(sm, sid, package, context).status == "committed"
        assert (
            sm.read_combat_snapshot(sid, package, context=context).character_states[0].hp_current
            == 3
        )
        assert sm.read_session(sid, package, context=context).character.current_hp == 3


def test_explicit_resource_and_casting_stocks_are_current_read_only_facts(
    tmp_path, package_data, context
):
    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=50)
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        pc = snapshot["character_states"][0]
        pc["spell_slots"] = {"1": 2}
        snapshot["resource_state"] = {
            "pools": [
                {
                    "owner_id": pc["entity_id"],
                    "pool_id": "pc.slot.1",
                    "kind": "spell_slot",
                    "level": 1,
                    "current": 2,
                    "maximum": 2,
                }
            ]
        }
        snapshot["casting_state"] = {
            "actors": [
                {
                    "actor_id": pc["entity_id"],
                    "verbal_available": True,
                    "sources": [],
                    "materials": [],
                }
            ]
        }
    package = _validate(data)
    context = replace(context, can_adjudicate_checks=True)
    with StateMachine(tmp_path / "stocks.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        assert (
            sm.submit_command(
                check_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        assert initialize(sm, sid, package, context).status == "committed"
        assert (
            sm.submit_command(attack(sm, sid, package, context), package, context=context).status
            == "committed"
        )
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.resource_state == before.resource_state
        assert after.casting_state == before.casting_state
        assert after.inventory_state == before.inventory_state
        assert after.character_states[0].spell_slots == {1: 2}


def test_noncombat_setup_is_rejected_instead_of_silently_dropped(package_data):
    assets = modern_loader()
    data = modern_data(package_data, assets)
    combat = data["rule_bindings"]["combat_foundation"]["snapshot"]["combat_state"]
    # Complete valid upstream setup; SM has not admitted combat.start persistence.
    ids = combat["initiative_ids"]
    data["rule_bindings"]["noncombat_foundation"]["snapshot"]["combat_setup"] = {
        "combat_id": "setup.test",
        "party_ids": combat["party_ids"],
        "encounter_ids": combat["encounter_ids"],
        "roll_order": ids,
        "actor_zone": combat["actor_zone"],
        "fixed_initiative": {i: 10 for i in ids},
        "initiative_modifiers": {i: None for i in ids},
        "surprised_ids": [],
        "xp_value_by_entity": combat["xp_value_by_entity"],
        "opportunity_attack_weapons": {},
        "npc_stat_blocks": [],
        "timed_activities": combat["timed_activities"],
        "persistent_areas": combat["persistent_areas"],
        "combat_objects": combat["combat_objects"],
    }
    with pytest.raises(StateMachineError) as error:
        _validate(data)
    assert error.value.code == "unsupported_noncombat_foundation"


def test_initialization_preparation_uses_one_consistent_read_scope(tmp_path, package_data, context):
    sm, sid, package, _, _ = setup(tmp_path, package_data, context, active=False)
    with sm:
        real = sm._prepare_combat
        calls = []

        def observed(row, approved):
            assert sm._conn.in_transaction and not sm._store._scope.write
            calls.append(row["world_version"])
            return real(row, approved)

        sm._prepare_combat = observed
        assert initialize(sm, sid, package, context).status == "committed"
        assert calls == [0]
