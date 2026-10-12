"""Compatibility entry for the composed combat availability query."""


class AvailabilityRuntime:
    def query_action_availability(self, session_id, actor_id, payload, package, *, context):
        return self._combat.query_action_availability(
            session_id, actor_id, payload, package, context=context
        )
