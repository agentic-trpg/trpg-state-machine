"""Schema47 life facts through public combat, rollback, and cold reopening."""

import json
import sqlite3

import pytest
from dnd_support import NPC
from test_dnd_runtime import attack, setup

from trpg_state_machine import StateMachine, StateMachineError


def _life(sm, sid, actor_id):
    """Read the one authoritative typed row from the active Store."""
    return sm._conn.execute(
        "SELECT * FROM mechanical_life WHERE session_id=? AND actor_id=?",
        (sid, actor_id),
    ).fetchone()


def test_attack_life_rows_rollback_and_reopen(tmp_path, package_data, context):
    def change(foundation):
        foundation["snapshot"]["character_states"][1].update(ac=10, hp_current=8, temp_hp=3)

    sm, sid, package, assets = setup(tmp_path, package_data, context, change)
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context)
        old = next(actor for actor in before.character_states if actor.entity_id == NPC)
        assert _life(sm, sid, NPC)["temp_hp"] == 3
        stored = sm._conn.execute(
            "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
            (sid, NPC),
        ).fetchone()[0]
        assert (
            not {"temp_hp", "is_alive", "death_saves", "last_damaged_by"}
            & json.loads(stored).keys()
        )
        command = attack(sm, sid, package, context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_life BEFORE UPDATE ON mechanical_life "
            "BEGIN SELECT RAISE(ABORT,'life fault'); END"
        )
        with pytest.raises(StateMachineError, match="SQLite transaction failed"):
            sm.submit_command(command, package, context=context)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert _life(sm, sid, NPC)["temp_hp"] == 3
        sm._conn.execute("DROP TRIGGER fail_life")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        current = next(actor for actor in after.character_states if actor.entity_id == NPC)
        life = _life(sm, sid, NPC)
        assert (life["temp_hp"], life["is_alive"], life["last_damaged_by"]) == (
            current.temp_hp,
            int(current.is_alive),
            current.last_damaged_by,
        )
        assert before.character_states != after.character_states
        assert next(actor for actor in before.character_states if actor.entity_id == NPC) == old
        assert sm.submit_command(command, package, context=context) == receipt
        assert (
            sm._conn.execute(
                "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
                (sid, NPC),
            ).fetchone()[0]
            == stored
        )
    with StateMachine(tmp_path / "dnd.db", dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after


def test_missing_life_row_and_database_bounds(tmp_path, package_data, context):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        with pytest.raises(sqlite3.IntegrityError):
            sm._conn.execute(
                "UPDATE mechanical_life SET temp_hp=-1 WHERE session_id=? AND actor_id=?",
                (sid, NPC),
            )
        with pytest.raises(sqlite3.IntegrityError):
            sm._conn.execute(
                "UPDATE mechanical_life SET death_save_successes=1 "
                "WHERE session_id=? AND actor_id=?",
                (sid, NPC),
            )
        with pytest.raises(sqlite3.IntegrityError):
            sm._conn.execute(
                "UPDATE mechanical_life SET death_save_failures=4.5 "
                "WHERE session_id=? AND actor_id=?",
                (sid, NPC),
            )
        sm._conn.execute(
            "DELETE FROM mechanical_life WHERE session_id=? AND actor_id=?", (sid, NPC)
        )
        with pytest.raises(StateMachineError, match="life component is missing"):
            sm.read_combat_snapshot(sid, package, context=context)


def test_nonnull_death_saves_are_typed_and_reopened(tmp_path, package_data, context):
    def change(foundation):
        foundation["snapshot"]["character_states"][1]["death_saves"] = {
            "successes": 1,
            "failures": 2,
            "is_stable": False,
        }

    sm, sid, package, assets = setup(tmp_path, package_data, context, change)
    with sm:
        snapshot = sm.read_combat_snapshot(sid, package, context=context)
        life = _life(sm, sid, NPC)
        assert tuple(
            life[name]
            for name in ("death_save_successes", "death_save_failures", "death_save_stable")
        ) == (1, 2, 0)
        assert (
            next(a for a in snapshot.character_states if a.entity_id == NPC).death_saves.failures
            == 2
        )
    with StateMachine(tmp_path / "dnd.db", dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == snapshot
