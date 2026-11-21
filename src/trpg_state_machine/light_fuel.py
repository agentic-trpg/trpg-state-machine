"""Metered ordinary light transitions and the existing WorldTime consumer port."""

from dataclasses import dataclass

from .errors import StateMachineError


@dataclass(frozen=True, slots=True)
class LightFuel:
    """Hold paused seconds or an active world tick. Both are absent for unmetered light."""

    remaining_seconds: int | None = None
    expires_tick: int | None = None


class LightFuelTimeConsumer:
    """Settle active light deadlines one object at a time in the world write."""

    def __init__(self, world):
        """Bind the authoritative WorldObject facts and light service."""
        self.world = world

    def next_boundary(self, session_id, package):
        """Get the earliest deadline on the existing world clock."""
        return self.world.facts.next_light_expiry(session_id)

    def settle_one(self, row, package, command_id):
        """Extinguish the current due lamp and capture its before/after evidence."""
        from .time_coordination import DueResult

        self.world.store.require_write()
        sid = row["session_id"]
        due = self.world.facts.due_light(sid, self.world.facts.time_state(sid)[1])
        if due is None:
            raise StateMachineError("invalid_time_consumer", "Missing due fuel boundary")
        oid, mode, deadline = due
        events = self.world.objects.lights.expire(sid, package, oid, mode, deadline)
        return DueResult(tuple(events))
