"""Local game-clock/1 and deterministic lifecycle of existing NPC Runtime entries."""

import json
from dataclasses import dataclass

from .errors import StateMachineError
from .package import canonical_json


@dataclass(frozen=True, slots=True)
class AdvanceGameTime:
    expected_tick: int
    next_tick: int


def advance_game_time(runtime, row, operation):
    sid = row["session_id"]
    tick = runtime._conn.execute(
        "SELECT game_tick FROM world_states WHERE session_id=?", (sid,)
    ).fetchone()[0]
    if tick != operation.expected_tick:
        raise StateMachineError("game_time_conflict", "Game time precondition differs")
    if operation.next_tick == tick:
        return []
    runtime._conn.execute(
        "UPDATE world_states SET game_tick=? WHERE session_id=?", (operation.next_tick, sid)
    )
    return [("game.time_advanced", {"previous_tick": tick, "game_tick": operation.next_tick})]


def sync_npc_schedules(runtime, sid, command_id):
    """Called only inside the original world-command savepoint. Never starts EVA."""
    db = runtime._conn
    tick = db.execute("SELECT game_tick FROM world_states WHERE session_id=?", (sid,)).fetchone()[0]
    if tick == 0:
        return []  # Preserve old initial_activity until the first authorized time advance.
    events = []
    for npc in db.execute(
        "SELECT n.*,a.current_scene_id FROM npc_states n JOIN actors a "
        "ON a.session_id=n.session_id AND a.actor_id=n.npc_id "
        "WHERE n.session_id=? AND n.runtime_json IS NOT NULL ORDER BY n.npc_id",
        (sid,),
    ).fetchall():
        data = json.loads(npc["runtime_json"])
        entries = {e["schedule_id"]: e for e in data["schedule"]}
        runs = {
            r["schedule_id"]: r
            for r in db.execute(
                "SELECT * FROM npc_schedule_runs WHERE session_id=? AND npc_id=?",
                (sid, npc["npc_id"]),
            ).fetchall()
        }
        # These are already committed mechanical facts, not HP/death computations.
        mechanical = db.execute(
            "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
            (sid, npc["npc_id"]),
        ).fetchone()
        state = json.loads(mechanical[0]) if mechanical else None
        available = state is None or (state["is_alive"] and not state["conditions"])
        changed = False

        def record(entry, status, previous=None, *, npc=npc):
            nonlocal changed
            if previous is None:
                db.execute(
                    "INSERT INTO npc_schedule_runs VALUES (?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        npc["npc_id"],
                        entry["schedule_id"],
                        status,
                        tick if status == "active" else None,
                        None if status == "active" else tick,
                        tick,
                        command_id,
                    ),
                )
            else:
                db.execute(
                    "UPDATE npc_schedule_runs SET status=?,finished_tick=?,updated_tick=?,"
                    "source_command_id=? WHERE session_id=? AND npc_id=? AND schedule_id=?",
                    (status, tick, tick, command_id, sid, npc["npc_id"], entry["schedule_id"]),
                )
            events.append(
                (
                    "npc.schedule_" + status,
                    {
                        "npc_id": npc["npc_id"],
                        "schedule_id": entry["schedule_id"],
                        "game_tick": tick,
                        "scene_id": npc["current_scene_id"],
                    },
                )
            )
            changed = True

        active = next((r for r in runs.values() if r["status"] == "active"), None)
        if active:
            entry = entries[active["schedule_id"]]
            if not available or entry["scene_id"] != npc["current_scene_id"]:
                record(entry, "interrupted", active)
                data["current_activity"] = None
                active = None
            elif tick >= entry["end_tick"]:
                record(entry, "completed", active)
                data["current_activity"] = None
                active = None
        for entry in entries.values():
            if entry["schedule_id"] not in runs and tick >= entry["end_tick"]:
                record(entry, "missed")
        candidates = [
            e
            for e in entries.values()
            if e["schedule_id"] not in runs
            and e["start_tick"] <= tick < e["end_tick"]
            and e["scene_id"] == npc["current_scene_id"]
            and available
        ]
        if len(candidates) + bool(active) > 1:
            raise StateMachineError(
                "unsupported_schedule", "Overlapping activities need reviewed policy"
            )
        if candidates:
            record(candidates[0], "active")
            data["current_activity"] = candidates[0]["activity"]
        if changed:
            db.execute(
                "UPDATE npc_states SET runtime_json=?,npc_state_version=npc_state_version+1 "
                "WHERE session_id=? AND npc_id=?",
                (canonical_json(data), sid, npc["npc_id"]),
            )
            # An EVA based on the previous Runtime version cannot commit stale Meta.
            # Mode and Observation work remain unchanged; the Host can request a fresh Task.
            db.execute(
                "UPDATE npc_evaluation_tasks SET status='cancelled',last_error='schedule_changed' "
                "WHERE session_id=? AND npc_id=? AND status='running'",
                (sid, npc["npc_id"]),
            )
    return events
