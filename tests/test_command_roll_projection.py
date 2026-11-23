"""Public receipt-scoped dice projection over persisted RE events."""

import json
import sqlite3

import pytest
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_srd_data import BundledAssetLoader
from item_support import NPC
from lock_support import CRED, DOOR, A
from spatial_support import create
from test_faerie_fire import _cast
from test_lockable_passages import acquire
from test_openable_checks import check_data
from test_scoped_runtime_validation import measure
from test_spell_attack_cast import OTHER_ENEMY
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    StateMachine,
    StateMachineError,
    TrustedContext,
    world_command_from_data,
)
from trpg_state_machine.package import _validate


def _force_session(tmp_path, context):
    """Create a crowbar force attempt that uses two d20 faces."""
    data, loader = check_data(jammed=True)
    next(row for row in data["item_transfers"] if row["instance_id"] == CRED)["item_slug"] = (
        "crowbar"
    )
    method = data["world_objects"][0]["capabilities"][0]["force_methods"][0]
    method.update(required_tool="crowbar", leverage_applicable=True)
    package = _validate(data)
    path = tmp_path / "rolls.sqlite"
    sm = StateMachine(path, dnd_loader=loader)
    sid = create(sm, package, context)
    acquire(sm, sid, package, context, iid=CRED, key="tool")
    return sm, sid, package, loader, path


def _force_command(sm, sid, package, context):
    """Decode an actual JSON world.force_open command."""
    state = sm.read_session(sid, package, context=context)
    return world_command_from_data(
        json.loads(
            json.dumps(
                {
                    "schema_version": "sm-command/1",
                    "session_id": sid,
                    "command_id": "force.roll",
                    "actor_id": state.pc_actor_id,
                    "command_type": "world.force_open",
                    "expected_world_version": state.world_version,
                    "payload": {
                        "scene_id": A,
                        "object_id": DOOR,
                        "method_id": "free-jam",
                        "tool_instance_id": CRED,
                        "expected_cell": "0,0",
                    },
                }
            )
        )
    )


def test_force_roll_is_persisted_projected_reopened_and_replayed(tmp_path, context):
    sm, sid, package, loader, path = _force_session(tmp_path, context)
    with sm:
        command = _force_command(sm, sid, package, context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        projected = sm.get_command_rolls(sid, command.command_id, package, context=context)
        assert projected["status"] == "committed"
        assert len(projected["rolls"]) == 1
        (roll,) = projected["rolls"]
        assert roll["type"] == "check_rolled" and roll["advantage"] == "advantage"
        event = sm._read_event(sid, roll["event_seq"])
        engine = event.payload["engine_event"]
        assert roll["roll_detail"] == engine.get("roll_detail")
        assert roll["natural"] == engine["natural"]
        assert roll["roll_total"] == engine["roll_total"]
        assert roll["roll_detail"]["status"] == "rolled"
        dice = roll["roll_detail"]["d20"]
        assert len(dice["faces"]) == 2
        assert dice["faces"][dice["kept_index"]] == roll["natural"]
        for modifier in roll["roll_detail"]["modifiers"]:
            assert modifier["kind"] in ("fixed", "die")
            assert isinstance(modifier["source"], str) and modifier["source"]
            assert type(modifier["value"]) is int
            if modifier["kind"] == "die":
                assert modifier["sides"] > 0
                assert 1 <= modifier["face"] <= modifier["sides"]
        rng_after = sm.read_rng_context(sid, package, context=context)
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_rng_context(sid, package, context=context) == rng_after
        assert sm.get_command_rolls(sid, command.command_id, package, context=context) == projected
        assert sm.get_command_rolls(sid, "unknown", package, context=context) is None
    with StateMachine(path, dnd_loader=loader) as reopened:
        assert (
            reopened.get_command_rolls(sid, command.command_id, package, context=context)
            == projected
        )


def test_faerie_rolls_follow_two_creature_targets(timed_spell_world):
    def configure(data):
        """Admit two targets in the cube, with one under the Restrained condition."""
        data["rule_bindings"].pop("combat_foundation")
        data["actors"][0]["initial_cell"] = "2,0"
        for actor in data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"]:
            if actor["entity_id"] == OTHER_ENEMY:
                actor["conditions"] = [
                    ActiveCondition(
                        condition="restrained",
                        source_entity_id="implied:condition",
                        scope="session",
                    ).model_dump(mode="json")
                ]

    session = timed_spell_world(
        database="faerie-projection.sqlite",
        caster_class="druid",
        caster_subclass="land",
        additional_spells=("faerie-fire",),
        additional_conditions=(BundledAssetLoader().get_condition("restrained"),),
        hit_rider="ray-of-frost",
        configure=configure,
    )
    sm, sid, package, context = session
    with sm:
        rng_before = sm.read_rng_context(sid, package, context=context)
        command, receipt = _cast(session, "faerie.roll")
        assert receipt.status == "committed"
        projected = sm.get_command_rolls(sid, command.command_id, package, context=context)
        rolls = projected["rolls"]
        assert len(rolls) == 2
        assert all(roll["type"] == "save_rolled" for roll in rolls)
        assert [roll["target_id"] for roll in rolls] == [OTHER_ENEMY, NPC]
        assert [roll["advantage"] for roll in rolls] == ["disadvantage", "normal"]
        assert [roll["roll_detail"]["d20"]["faces"] for roll in rolls] == [[13, 14], [2]]
        assert [roll["natural"] for roll in rolls] == [13, 2]
        assert [roll["event_seq"] for roll in rolls] == sorted(roll["event_seq"] for roll in rolls)
        for roll in rolls:
            stored = sm._read_event(sid, roll["event_seq"])
            assert roll["roll_detail"] == stored.payload["engine_event"].get("roll_detail")
            assert roll["roll_detail"]["status"] == "rolled"
        failed = {roll["target_id"] for roll in rolls if not roll["succeeded"]}
        with sm._transaction():
            affected = {
                row[0]
                for row in sm._conn.execute(
                    "SELECT target_id FROM target_visual_effects "
                    "WHERE session_id=? AND target_kind='creature'",
                    (sid,),
                )
            }
        assert affected == failed
        rng_after = sm.read_rng_context(sid, package, context=context)
        assert rng_after.version == rng_before.version + 1
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_rng_context(sid, package, context=context) == rng_after
        assert sm.get_command_rolls(sid, command.command_id, package, context=context) == projected


def test_roll_projection_checks_session_and_revoked_actor(tmp_path, context):
    sm, sid, package, _, _ = _force_session(tmp_path, context)
    with sm:
        command = _force_command(sm, sid, package, context)
        rng_before = sm.read_rng_context(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_roll_receipt BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(command, package, context=context)
        assert sm.get_command_rolls(sid, command.command_id, package, context=context) is None
        assert sm.read_rng_context(sid, package, context=context) == rng_before
        sm._conn.execute("DROP TRIGGER fail_roll_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        other = TrustedContext("authenticated:bob", "player.bob", ())
        with pytest.raises(StateMachineError, match="Context does not control"):
            sm.get_command_rolls(sid, command.command_id, package, context=other)
        with sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE command_receipts SET receipt_json=json_set(receipt_json, '$.actor_id', ?) "
                "WHERE session_id=? AND command_id=?",
                ("npc.revoked", sid, command.command_id),
            )
        with pytest.raises(StateMachineError, match="Actor control grant has been revoked"):
            sm.get_command_rolls(sid, command.command_id, package, context=context)


def test_older_persisted_roll_without_evidence_reports_absence(tmp_path, context):
    sm, sid, package, loader, path = _force_session(tmp_path, context)
    with sm:
        command = _force_command(sm, sid, package, context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        seq = receipt.event_seqs[0]
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE events SET payload_json="
            "json_remove(payload_json, '$.engine_event.roll_detail') "
            "WHERE session_id=? AND event_seq=?",
            (sid, seq),
        )
    with StateMachine(path, dnd_loader=loader) as reopened:
        projected = reopened.get_command_rolls(sid, command.command_id, package, context=context)
        assert projected["rolls"][0]["roll_detail"] is None
        assert projected["rolls"][0]["roll_total"] is not None


def test_roll_projection_reads_only_receipt_events_as_history_grows(tmp_path, context):
    sm, sid, package, _, _ = _force_session(tmp_path, context)
    with sm:
        command = _force_command(sm, sid, package, context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        source_seq = receipt.event_seqs[0]
        observed = []
        for growth, start in ((1, 10000), (50, 10001)):
            with sm._transaction(write=True):
                for seq in range(start, start + growth):
                    sm._conn.execute(
                        "INSERT INTO events SELECT session_id, ?, event_type, world_version, "
                        "payload_json FROM events WHERE session_id=? AND event_seq=?",
                        (seq, sid, source_seq),
                    )
                    sm._conn.execute(
                        "INSERT INTO event_records SELECT session_id, ?, ?, ?, signals_json "
                        "FROM event_records WHERE session_id=? AND event_seq=?",
                        (seq, f"event.{sid}.{seq}", "unrelated.command", sid, source_seq),
                    )
            with measure(sm) as (counts, statements):
                result = sm.get_command_rolls(sid, command.command_id, package, context=context)
            assert len(result["rolls"]) == 1
            assert not any("event_seq>?" in sql for sql in statements)
            observed.append(
                tuple(counts[key] for key in ("sql_selects", "sql_rows", "returned_bytes"))
            )
        assert observed[0] == observed[1]
