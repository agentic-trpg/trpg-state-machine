"""Historical queue backfill, only inside the locked schema migration."""

from .event_persistence import EventPersistence, _enqueue_perception_proof
from .package import RulesetBinding


class _QueueMigration:
    _read_event = EventPersistence._read_event

    def __init__(self, connection):
        self._conn = connection

    def _enqueue_perception(self, sid, seq, binding):
        return _enqueue_perception_proof(self._conn, self._read_event, sid, seq, binding)

    def _migrate_perception_queues(self):
        for session in self._conn.execute("SELECT * FROM sessions").fetchall():
            binding = RulesetBinding(
                session["ruleset_id"], session["data_revision"], session["evaluator_version"]
            )
            for (seq,) in self._conn.execute(
                "SELECT event_seq FROM event_records WHERE session_id=? ORDER BY event_seq",
                (session["session_id"],),
            ).fetchall():
                self._enqueue_perception(session["session_id"], seq, binding)


def migrate_perception_queues(connection):
    _QueueMigration(connection)._migrate_perception_queues()
