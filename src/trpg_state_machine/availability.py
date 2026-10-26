"""Compatibility entry for the composed combat availability query."""


class AvailabilityRuntime:
    """Expose the command runtime's delegated rules availability entrypoint."""

    def query_action_availability(self, session_id, actor_id, payload, package, *, context):
        """Delegate a read-only action availability query to the combat handler."""
        return self._combat.query_action_availability(
            session_id, actor_id, payload, package, context=context
        )
