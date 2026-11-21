"""Refill a bound retained item at its next configured world dawn."""

from .errors import StateMachineError
from .time_binding import TimeBinding
from .world_time import WorldTimeService


def next_item_dawn(binding, tick):
    """Return the next configured day start strictly after the current tick."""
    if type(binding) is not TimeBinding or binding.day_start is None:
        raise StateMachineError(
            "unsupported_time_protocol", "Item recharge needs an alternating world-time calendar"
        )
    return WorldTimeService._next(binding, tick, binding.day_start)


class ItemRechargeTimeConsumer:
    """Refill one due instance per step in the same world-time write."""

    def __init__(self, world):
        """Bind recovery to the existing item facts and event capture."""
        self.world = world

    def next_boundary(self, session_id, package):
        """Return the earliest pending item dawn within the playable tick range."""
        return self.world.facts.next_item_recharge(session_id)

    def settle_one(self, row, package, command_id):
        """Refill the selected instance and capture its event at the settled tick."""
        from .time_coordination import DueResult

        sid = row["session_id"]
        self.world.store.require_write()
        _, tick = self.world.facts.time_state(sid)
        before = self.world.facts.due_item_recharge(sid, tick)
        if before is None:
            raise StateMachineError("invalid_time_consumer", "Missing due item recovery")
        self.world.facts.recover_item_charges(sid, before)
        event = self.world._capture_event(
            sid,
            package,
            "item.charges_recovered",
            {
                "instance_id": before["object_id"],
                "previous_charges": before["item_charges"],
                "charges": before["item_recharge_capacity"],
                "boundary_tick": before["item_recharge_due_tick"],
            },
            phase="world.after_item_recharge",
        )
        return DueResult((event,))
