"""Persist exploration casting progress and wait at its exact completion tick."""

from dataclasses import dataclass

from dnd5e_engine.evaluation_identify import IdentifyObjectFacts
from dnd5e_engine.evaluation_state import InventoryEntry
from dnd5e_engine.events import ObjectIdentification

from .errors import StateMachineError
from .time_binding import deadline_after_seconds, integer
from .time_waits import DomainTimeWait, TimeWaitResolution

CASTING_FOCUS_PREFIX = "cast.activity:"


@dataclass(frozen=True, slots=True)
class StartSpellCast:
    """Select an exploration spell source and any required object and material."""

    activity_id: str
    spell_id: str
    spell_source_id: str
    slot_level: int | None = None
    as_ritual: bool = False
    target_object_id: str | None = None
    material_instance_id: str | None = None


@dataclass(frozen=True, slots=True)
class FinishSpellCast:
    """Select the casting activity named by this command's time-wait reference."""

    activity_id: str


@dataclass(frozen=True, slots=True)
class SpellCastActivity:
    """An authorized view of committed progress and any completed identification."""

    activity_id: str
    actor_id: str
    spell_id: str
    spell_source_id: str
    slot_level: int | None
    as_ritual: bool
    start_tick: int
    deadline_tick: int
    status: str
    activity_version: int
    target_object_id: str | None = None
    material_instance_id: str | None = None
    identification: ObjectIdentification | None = None


def validate_spell_cast_activity(operation):
    """Check closed command fields without admitting the selected spell or payment."""
    identifiers = (operation.activity_id,)
    if type(operation) is StartSpellCast:
        identifiers += (operation.spell_id, operation.spell_source_id)
        selections = (operation.target_object_id, operation.material_instance_id)
        if any(value is not None for value in selections):
            identifiers += selections
        if type(operation.as_ritual) is not bool:
            raise StateMachineError("invalid_operation", "Ritual selection must be boolean")
        if operation.as_ritual:
            if operation.slot_level is not None:
                raise StateMachineError("invalid_operation", "A Ritual selects no spell slot")
        else:
            integer(
                operation.slot_level, "slot_level", minimum=1, maximum=9, code="invalid_operation"
            )
    if any(
        type(value) is not str or not value.strip() or len(value) > 256 for value in identifiers
    ):
        raise StateMachineError("invalid_operation", "Casting identifiers must be bounded strings")
    try:
        for value in identifiers:
            value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise StateMachineError("invalid_operation", "Invalid Unicode casting identifier") from exc


def casting_focus_id(activity_id):
    """Derive the exact Effects focus identity linked to the casting activity."""
    return CASTING_FOCUS_PREFIX + activity_id


class SpellCastActivities:
    """Own casting progress and its domain time wait in the shared Store."""

    def __init__(self, world):
        """Bind the same World and Store used to record effects and clock changes."""
        self.world = world
        self.store = world.store

    @property
    def db(self):
        """Return the connection in the caller's existing transaction."""
        return self.store.connection

    def get(self, sid, activity_id):
        """Read one activity by its Session and stable activity ID."""
        return self.db.execute(
            "SELECT * FROM spell_cast_activities WHERE session_id=? AND activity_id=?",
            (sid, activity_id),
        ).fetchone()

    def active(self, sid, actor_id):
        """Read the actor's unfinished cast, including a cast awaiting adoption."""
        return self.db.execute(
            "SELECT * FROM spell_cast_activities WHERE session_id=? AND actor_id=? "
            "AND status IN ('casting', 'waiting')",
            (sid, actor_id),
        ).fetchone()

    @staticmethod
    def snapshot(record):
        """Detach progress for a caller whose activity access has been authorized."""
        return SpellCastActivity(
            activity_id=record["activity_id"],
            actor_id=record["actor_id"],
            spell_id=record["spell_id"],
            spell_source_id=record["spell_source_id"],
            slot_level=record["slot_level"],
            as_ritual=bool(record["as_ritual"]),
            start_tick=record["start_tick"],
            deadline_tick=record["deadline_tick"],
            status=record["status"],
            activity_version=record["activity_version"],
            target_object_id=record["target_object_id"],
            material_instance_id=record["material_instance_id"],
            identification=(
                ObjectIdentification.model_validate_json(record["identification_json"])
                if record["identification_json"] is not None
                else None
            ),
        )

    def identify_facts(self, sid, actor_id, target_id, material_id, package, *, starting=False):
        """Read the exact reachable item and retained pearl through the World owners."""
        try:
            target = self.world.objects.containers.reachable(
                sid, actor_id, target_id, package, require_visible_contact=starting
            )
        except StateMachineError as exc:
            if exc.code in {
                "invalid_target",
                "unsupported_target_visibility",
                "unsupported_interaction_scope",
                "container_inaccessible",
                "object_out_of_reach",
            }:
                return None
            raise
        facts = self.world.facts
        material = facts.object(sid, material_id)
        entry = facts.item_entry(material) if material and material["item_slug"] else None
        if (
            entry is None
            or entry["owner_id"] != actor_id
            or not entry["accessible"]
            or not target["item_slug"]
        ):
            return None
        unit = facts.require_item_unit(target)
        if unit["quantity"] != 1 or not unit["accessible"]:
            return None
        return IdentifyObjectFacts(
            target_id=target_id,
            item_slug=unit["item_slug"],
            charges_remaining=unit["charges_remaining_per_unit"],
            ongoing_spell_ids=self.world.magic.object_spells(sid, target_id),
            material=InventoryEntry.model_validate(entry),
        )

    def interrupt_lost_contact(self, row, package, command_id):
        """Check the selected PC's indexed active cast against the candidate world."""
        sid, actor_id = row["session_id"], row["pc_actor_id"]
        record = self.active(sid, actor_id)
        if record is None or record["target_object_id"] is None:
            return ()
        if (
            self.world._active_combat(sid)
            or self.identify_facts(
                sid, actor_id, record["target_object_id"], record["material_instance_id"], package
            )
            is None
        ):
            return (self.interrupt(record, package, command_id),)
        return ()

    def record_identification(self, sid, activity_id, identification):
        """Keep resolved knowledge in the authorized Activity read, inside the finish scope."""
        self.store.require_write()
        changed = self.db.execute(
            "UPDATE spell_cast_activities SET identification_json=? WHERE session_id=? "
            "AND activity_id=? AND status='waiting' AND target_object_id=? "
            "AND identification_json IS NULL",
            (
                identification.model_dump_json(),
                sid,
                activity_id,
                identification.target_id,
            ),
        ).rowcount
        if changed != 1:
            raise StateMachineError("invalid_rule_result", "Identify result has no waiting cast")

    def _event(self, record, package, result):
        """Capture the actual activity transition without publishing cast effects."""
        return self.world._capture_event(
            record["session_id"],
            package,
            "activity.spell_cast_" + result,
            {
                "activity_id": record["activity_id"],
                "actor_id": record["actor_id"],
                "spell_id": record["spell_id"],
                "activity_version": record["activity_version"],
                "start_tick": record["start_tick"],
                "deadline_tick": record["deadline_tick"],
                "status": record["status"],
            },
            phase="activity.after_progress",
            source_actor_id=record["actor_id"],
        )

    def start(self, row, package, actor_id, operation, *, seconds, concentration_id, command_id):
        """Record the authorized start using the bound duration and adopted Effects focus."""
        self.store.require_write()
        sid = row["session_id"]
        if self.get(sid, operation.activity_id) is not None:
            raise StateMachineError("activity_exists", "Activity identity is historical")
        if self.active(sid, actor_id) is not None:
            raise StateMachineError("activity_conflict", "Actor already has a casting activity")
        binding, tick = self.world.facts.time_state(sid)
        deadline = deadline_after_seconds(binding, tick, seconds)
        self.db.execute(
            "INSERT INTO spell_cast_activities (session_id,activity_id,actor_id,spell_id,"
            "spell_source_id,slot_level,as_ritual,start_tick,deadline_tick,status,activity_version,"
            "concentration_id,source_command_id,last_command_id,"
            "target_object_id,material_instance_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,'casting',1,?,?,?,?,?)",
            (
                sid,
                operation.activity_id,
                actor_id,
                operation.spell_id,
                operation.spell_source_id,
                operation.slot_level,
                operation.as_ritual,
                tick,
                deadline,
                concentration_id,
                command_id,
                command_id,
                operation.target_object_id,
                operation.material_instance_id,
            ),
        )
        return self._event(self.get(sid, operation.activity_id), package, "started")

    def _transition(self, record, status, command_id, *, concentration_id):
        """Apply one guarded Activity transition inside its caller's write scope."""
        self.store.require_write()
        sid, aid = record["session_id"], record["activity_id"]
        changed = self.db.execute(
            "UPDATE spell_cast_activities SET status=?,activity_version=activity_version+1,"
            "concentration_id=?,last_command_id=? WHERE session_id=? AND activity_id=? "
            "AND status=? AND activity_version=?",
            (
                status,
                concentration_id,
                command_id,
                sid,
                aid,
                record["status"],
                record["activity_version"],
            ),
        ).rowcount
        if changed != 1:
            raise StateMachineError("activity_version_conflict", "Casting progress changed")
        return self.get(sid, aid)

    def interrupt(self, record, package, command_id):
        """End an in-progress cast and its focus while keeping committed clock progress."""
        if record["status"] not in ("casting", "waiting"):
            raise StateMachineError("activity_inactive", "Casting is already finished")
        updated = self._transition(record, "cancelled", command_id, concentration_id=None)
        self.world.characters.effects.remove_concentration(
            record["session_id"], record["concentration_id"]
        )
        return self._event(updated, package, "interrupted")

    def next_boundary(self, sid, package):
        """Return the next unfinished casting deadline using the partial deadline index."""
        return self.db.execute(
            "SELECT min(deadline_tick) FROM spell_cast_activities "
            "WHERE session_id=? AND status='casting'",
            (sid,),
        ).fetchone()[0]

    def settle_one(self, row, package, command_id):
        """Stage an exact completion wait without invoking rules in this write transaction."""
        from .time_coordination import DueResult

        sid = row["session_id"]
        _, tick = self.world.facts.time_state(sid)
        record = self.db.execute(
            "SELECT * FROM spell_cast_activities WHERE session_id=? AND status='casting' "
            "AND deadline_tick<=? ORDER BY deadline_tick,activity_id LIMIT 1",
            (sid, tick),
        ).fetchone()
        if record is None or record["deadline_tick"] != tick:
            raise StateMachineError("invalid_time_consumer", "Casting completion tick differs")
        if (
            record["target_object_id"] is not None
            and self.identify_facts(
                sid,
                record["actor_id"],
                record["target_object_id"],
                record["material_instance_id"],
                package,
            )
            is None
        ):
            return DueResult(events=(self.interrupt(record, package, command_id),))
        updated = self._transition(
            record, "waiting", command_id, concentration_id=record["concentration_id"]
        )
        return DueResult(
            events=(self._event(updated, package, "ready"),),
            status="waiting",
            reason="spell_cast_completion",
            wait=DomainTimeWait(updated["activity_id"], updated["activity_version"]),
        )

    def read_time_wait(self, sid, wait_id, package):
        """Project domain wait state, retaining terminal states for receipt replay."""
        record = self.get(sid, wait_id)
        if record is None or record["status"] == "casting":
            return None
        status = "resolved" if record["status"] == "completed" else record["status"]
        return DomainTimeWait(wait_id, record["activity_version"], status)

    def authorize_time_wait_command(self, row, package, command, context, wait):
        """Restrict a prior casting wait to the selected caster and its typed reply."""
        record = self.get(row["session_id"], wait.wait_id)
        finish = (
            type(command.payload) is FinishSpellCast and command.payload.activity_id == wait.wait_id
        )
        if (
            record is None
            or record["actor_id"] != command.actor_id
            or command.actor_id != row["pc_actor_id"]
            or not (finish or command.command_type == "world.drop_concentration")
        ):
            raise StateMachineError("unauthorized", "Casting wait does not authorize this reply")

    def resolve_time_wait(self, row, package, command, wait, *, events):
        """Transition and release focus after adopting the authorized completion in scope."""
        sid = row["session_id"]
        record = self.get(sid, wait.wait_id)
        if self.world._active_combat(sid):
            raise StateMachineError("invalid_time_wait_result", "Casting wait requires exploration")
        if command.command_type == "world.drop_concentration":
            return TimeWaitResolution(
                "cancel", (self.interrupt(record, package, command.command_id),)
            )
        if type(command.payload) is not FinishSpellCast or not any(
            event.event_type == "world.spell_cast"
            and evidence.get("type") == "spell_cast"
            and evidence.get("actor_id") == record["actor_id"]
            and evidence.get("spell_id") == record["spell_id"]
            for event in events
            for evidence in (event.payload.get("engine_event", {}),)
        ):
            raise StateMachineError("invalid_time_wait_result", "Spell resolution was not adopted")
        if record["target_object_id"] is not None and record["identification_json"] is None:
            raise StateMachineError("invalid_time_wait_result", "Identify result was not adopted")
        updated = self._transition(record, "completed", command.command_id, concentration_id=None)
        self.world.characters.effects.remove_concentration(
            record["session_id"], record["concentration_id"]
        )
        return TimeWaitResolution("continue", (self._event(updated, package, "completed"),))
