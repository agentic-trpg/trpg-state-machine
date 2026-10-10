"""Reliable output dispatch using the existing command/receipt authority, without ACK."""

import json
from dataclasses import dataclass

from dnd5e_engine.evaluation_contracts import CombatIntentPayload

from ._operations import MoveActor
from .commands import CommandReceipt, TypedCommand, receipt_from_json
from .errors import StateMachineError
from .models import TrustedContext
from .npc_results import NPCActionIntent, NPCCombatIntent, NPCDialogue


def _handoff_command(row):
    data = json.loads(row["payload_json"])
    if "payload" in data:
        intent = NPCCombatIntent(
            CombatIntentPayload.model_validate_json(json.dumps(data["payload"])),
            data["expected_world_version"],
        )
        kind, payload = "combat.intent", intent.payload
    else:
        intent = NPCActionIntent(**data)
        kind, payload = "move_actor", MoveActor(row["npc_id"], intent.destination_scene_id)
    return TypedCommand(
        row["session_id"],
        row["command_id"],
        row["npc_id"],
        kind,
        payload,
        intent.expected_world_version,
    )


@dataclass(frozen=True, slots=True)
class NPCOutputDelivery:
    ordinal: int
    kind: str
    command_id: str | None
    status: str
    error_code: str | None
    command_receipt: CommandReceipt | None
    published_event_seq: int | None


class NPCOutputRuntime:
    def _refresh_npc_completion(self, sid):
        projected = self._conn.execute(
            "SELECT event_seq FROM projection_cursors WHERE session_id=? AND consumer='perception'",
            (sid,),
        ).fetchone()
        end = projected[0] if projected else 0
        for actor in self._conn.execute(
            "SELECT npc_id FROM npc_states WHERE session_id=?", (sid,)
        ).fetchall():
            pending = self._conn.execute(
                "SELECT MIN(o.event_seq) FROM npc_observation_work w "
                "JOIN event_observations o USING(observation_id) "
                "WHERE w.session_id=? AND w.npc_id=? AND w.status='pending' AND o.event_seq<=?",
                (sid, actor[0], end),
            ).fetchone()[0]
            cursor = pending - 1 if pending is not None else end
            self._conn.execute(
                "INSERT INTO npc_completion_cursors VALUES (?,?,?) "
                "ON CONFLICT(session_id,npc_id) DO UPDATE SET "
                "event_seq=max(npc_completion_cursors.event_seq,excluded.event_seq)",
                (sid, actor[0], cursor),
            )

    def _complete_npc_evaluation_work(self, row):
        self._conn.execute(
            "UPDATE npc_observation_work SET status='completed',disposition='evaluation' "
            "WHERE status='pending' AND work_id IN "
            "(SELECT work_id FROM npc_evaluation_observations WHERE evaluation_id=?)",
            (row["evaluation_id"],),
        )
        self._refresh_npc_completion(row["session_id"])

    def npc_completed_cursor(self, session_id, package, *, context):
        """Internal processing prefix in event order, independent of read/delivery cursors."""
        with self._transaction():
            npc = self._npc_authorized(session_id, package, context)
            row = self._conn.execute(
                "SELECT event_seq FROM npc_completion_cursors WHERE session_id=? AND npc_id=?",
                (session_id, npc),
            ).fetchone()
            return row[0] if row else 0

    def _output_delivery(self, row):
        receipt = self._conn.execute(
            "SELECT receipt_json FROM command_receipts WHERE session_id=? AND command_id=?",
            (row["session_id"], row["command_id"]),
        ).fetchone()
        return NPCOutputDelivery(
            row["ordinal"],
            row["kind"],
            row["command_id"],
            row["status"],
            row["error_code"],
            receipt_from_json(receipt[0]) if receipt else None,
            row["event_seq"],
        )

    def get_npc_output_status(self, evaluation_id, package, *, context):
        with self._transaction():
            task = self._evaluation_row(evaluation_id, package, context)
            return tuple(
                self._output_delivery(row)
                for row in self._conn.execute(
                    "SELECT h.*,t.session_id FROM npc_output_handoffs h "
                    "JOIN npc_evaluation_tasks t USING(evaluation_id) "
                    "WHERE h.evaluation_id=? ORDER BY ordinal",
                    (task["evaluation_id"],),
                ).fetchall()
            )

    def _dispatch_authorized(self, evaluation_id, ordinal, package, context):
        if type(context) is not TrustedContext:
            raise StateMachineError("unauthorized", "Dispatch requires trusted command control")
        if type(evaluation_id) is not str or type(ordinal) is not int or not 0 <= ordinal <= 100:
            raise StateMachineError("invalid_evaluation", "Invalid output identity")
        row = self._conn.execute(
            "SELECT h.*,t.session_id,t.npc_id,t.principal_id,t.status AS task_status "
            "FROM npc_output_handoffs h JOIN npc_evaluation_tasks t USING(evaluation_id) "
            "WHERE h.evaluation_id=? AND h.ordinal=?",
            (evaluation_id, ordinal),
        ).fetchone()
        if row is None:
            raise StateMachineError("unauthorized", "No authorized output")
        session = self._session(row["session_id"], package, context)
        grant = self._conn.execute(
            "SELECT principal_id FROM npc_access WHERE session_id=? AND observer_id=?",
            (row["session_id"], row["npc_id"]),
        ).fetchone()
        if (
            grant is None
            or grant[0] != row["principal_id"]
            or row["npc_id"] not in context.controlled_npc_ids
        ):
            raise StateMachineError(
                "unauthorized", "Output requires current NPC controller and command grant"
            )
        if row["task_status"] != "completed":
            raise StateMachineError("evaluation_inactive", "No completed Result handoff")
        return row, session

    def _npc_handoff_command_guard(self, command):
        """Recheck persisted origin under the existing command's world write lock."""
        row = self._conn.execute(
            "SELECT h.command_id,h.payload_json,t.session_id,t.npc_id,t.principal_id "
            "FROM npc_output_handoffs h "
            "JOIN npc_evaluation_tasks t USING(evaluation_id) WHERE h.command_id=?",
            (command.command_id,),
        ).fetchone()
        if row is None:
            return
        grant = self._conn.execute(
            "SELECT principal_id FROM npc_access WHERE session_id=? AND observer_id=?",
            (row["session_id"], row["npc_id"]),
        ).fetchone()
        if grant is None or grant[0] != row["principal_id"]:
            raise StateMachineError("unauthorized", "NPC output controller grant was revoked")
        if command != _handoff_command(row):
            raise StateMachineError(
                "idempotency_conflict", "Command differs from its reliable handoff"
            )

    def dispatch_npc_output(self, evaluation_id, ordinal, package, *, context):
        """Action uses submit_command outside this transaction; lost replies query its receipt."""
        with self._transaction(write=True):
            row, session = self._dispatch_authorized(evaluation_id, ordinal, package, context)
            if row["status"] != "pending":
                return self._output_delivery(row)
            if row["kind"] == "dialogue":
                self._publish_npc_dialogue(row, session, package)
                updated, _ = self._dispatch_authorized(evaluation_id, ordinal, package, context)
                return self._output_delivery(updated)
            command = _handoff_command(row)
        # No Rule Engine, external I/O or nested command transaction under this lock.
        self.submit_command(command, package, context=context)
        with self._transaction(write=True):
            row, _ = self._dispatch_authorized(evaluation_id, ordinal, package, context)
            if row["status"] == "pending":
                self._conn.execute(
                    "UPDATE npc_output_handoffs SET status='dispatched' "
                    "WHERE evaluation_id=? AND ordinal=?",
                    (evaluation_id, ordinal),
                )
            updated, _ = self._dispatch_authorized(evaluation_id, ordinal, package, context)
            return self._output_delivery(updated)

    def _npc_combat_target_guard(self, command, package, snapshot):
        """Handoff visibility is deliberately limited to clear, bright current scenes."""
        if not self._conn.execute(
            "SELECT 1 FROM npc_output_handoffs WHERE command_id=?", (command.command_id,)
        ).fetchone():
            return
        if type(command.payload) is not CombatIntentPayload or command.payload.target_id is None:
            return
        definition = next(a for a in package.actors if a.actor_id == command.actor_id)
        locations = dict(
            self._conn.execute(
                "SELECT actor_id,current_scene_id FROM actors WHERE session_id=?",
                (command.session_id,),
            ).fetchall()
        )
        states = {a.entity_id: a for a in snapshot.character_states}
        grid = snapshot.scene_state.grid
        observer, target = states.get(command.actor_id), states.get(command.payload.target_id)
        if (
            definition.senses is None
            or not dict(definition.senses)["visual"]
            or command.payload.target_id not in locations
            or locations.get(command.actor_id) != locations.get(command.payload.target_id)
            or observer is None
            or target is None
            or observer.conditions
            or target.conditions
            or grid.default_lighting != "bright"
            or grid.lighting
            or grid.obscurement_cells
            or grid.wall_segments
            or grid.blocked_cells
            or grid.cover_cells
        ):
            raise StateMachineError(
                "unauthorized_target", "Target is outside the supported current perception scope"
            )

    def _publish_npc_dialogue(self, row, session, package):
        data = json.loads(row["payload_json"])
        data["audience_ids"] = tuple(data["audience_ids"])
        data["source_observation_ids"] = tuple(data["source_observation_ids"])
        dialogue = NPCDialogue(**data)
        error, status, seq = None, "private", None
        source_ids = {
            s[0]
            for s in self._conn.execute(
                "SELECT w.observation_id FROM npc_evaluation_observations e "
                "JOIN npc_observation_work w USING(work_id) WHERE e.evaluation_id=?",
                (row["evaluation_id"],),
            )
        }
        if not set(dialogue.source_observation_ids) <= source_ids:
            status, error = "rejected", "invalid_dialogue_source"
        elif dialogue.visibility == "private":
            if dialogue.audience_ids:
                status, error = "rejected", "unsupported_private_audience"
        else:
            scene = self._conn.execute(
                "SELECT current_scene_id FROM actors WHERE session_id=? AND actor_id=?",
                (row["session_id"], row["npc_id"]),
            ).fetchone()[0]
            audience = {
                a[0]
                for a in self._conn.execute(
                    "SELECT actor_id FROM actors WHERE session_id=? "
                    "AND current_scene_id=? AND actor_id!=?",
                    (row["session_id"], scene, row["npc_id"]),
                )
            }
            # Scene-scoped Signals cannot enforce a whisper/subset audience.
            if not audience or set(dialogue.audience_ids) != audience:
                status, error = "rejected", "unauthorized_dialogue_audience"
            else:
                self._append_events(
                    row["session_id"],
                    session["world_version"],
                    [
                        (
                            "npc.dialogue_published",
                            {
                                "scene_id": scene,
                                "speaker_id": row["npc_id"],
                                "text": dialogue.text,
                                "audience_ids": sorted(audience),
                            },
                        )
                    ],
                    source_command_id=f"dialogue.{row['evaluation_id']}.{row['ordinal']}",
                    package=package,
                )
                seq = self._conn.execute(
                    "SELECT event_seq FROM sessions WHERE session_id=?", (row["session_id"],)
                ).fetchone()[0]
                status = "published"
        self._conn.execute(
            "UPDATE npc_output_handoffs SET status=?,error_code=?,event_seq=? "
            "WHERE evaluation_id=? AND ordinal=?",
            (status, error, seq, row["evaluation_id"], row["ordinal"]),
        )
