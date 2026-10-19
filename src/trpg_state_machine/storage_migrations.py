"""Historical schema reconciliation/backfill inside the locked migration transaction."""

from .errors import StateMachineError
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


def _layout(connection, table):
    # Only bundled constant table names are passed by this module.
    return tuple(
        (r[1], r[2].upper(), r[3], r[5]) for r in connection.execute(f"PRAGMA table_info({table})")
    )


def _foreign_keys(connection, table):
    return {(r[2], r[3], r[4]) for r in connection.execute(f"PRAGMA foreign_key_list({table})")}


def _unknown_lineage():
    raise StateMachineError(
        "unsupported_storage_schema", "Unrecognized environment/mechanism schema"
    )


def validate_environment_table(connection, *, required=False):
    """Recognize the actual ENV table before reusing any historical rows.

    A matching user_version alone is insufficient. The old ENV34 and new ENV35
    share the exact table contract; no rewrite or default reimport is needed.
    """
    exists = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='scene_environments'"
    ).fetchone()
    if exists is None:
        if required:
            _unknown_lineage()
        return False
    if (
        exists[0] != "table"
        or _layout(connection, "scene_environments")
        != (
            ("session_id", "TEXT", 1, 1),
            ("scene_id", "TEXT", 1, 2),
            ("facts_json", "TEXT", 1, 0),
        )
        or _foreign_keys(connection, "scene_environments")
        != {
            ("sessions", "session_id", "session_id"),
        }
    ):
        _unknown_lineage()
    if connection.execute(
        "SELECT 1 FROM scene_environments WHERE NOT json_valid(facts_json) LIMIT 1"
    ).fetchone():
        _unknown_lineage()
    return True


def validate_environment_versions(connection, *, required):
    import json

    versions = _layout(connection, "scene_environment_versions")
    bindings = _layout(connection, "environment_provider_bindings")
    if not required:
        if versions or bindings:
            _unknown_lineage()
        return
    if any(
        _foreign_keys(connection, table) != {("sessions", "session_id", "session_id")}
        for table in ("scene_environment_versions", "environment_provider_bindings")
    ):
        _unknown_lineage()
    if versions != (
        ("session_id", "TEXT", 1, 1),
        ("scene_id", "TEXT", 1, 2),
        ("source_version", "INTEGER", 1, 0),
        ("protection_version", "INTEGER", 1, 0),
    ) or bindings != (
        ("session_id", "TEXT", 1, 1),
        ("scene_id", "TEXT", 1, 2),
        ("role", "TEXT", 1, 3),
        ("descriptor", "TEXT", 1, 0),
        ("manifest_json", "TEXT", 1, 0),
    ):
        _unknown_lineage()
    triggers = dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    required_names = {
        "env_" + kind + "_" + op
        for kind in ("facts", "grid", "membership")
        for op in ("insert", "update", "delete")
    }
    if not required_names <= triggers.keys():
        _unknown_lineage()
    if (
        not {
            "env_facts_scope_immutable",
            "env_grid_scope_immutable",
            "env_object_identity_immutable",
        }
        <= triggers.keys()
    ):
        _unknown_lineage()
    for (manifest,) in connection.execute(
        "SELECT manifest_json FROM environment_provider_bindings"
    ):
        expected = json.loads(manifest)
        if type(expected) is not dict or any(
            triggers.get(name) != sql for name, sql in expected.items()
        ):
            _unknown_lineage()


def reconcile_schema34(connection, version, mechanism_statements):
    """Reconcile the two shipped Schema34 lineages under the migration write lock.

    ENV34 has the ENV table and the original three-column mechanism table. Apply
    the unchanged main34 migration ONCE to that exact shape, then let migration35
    reuse the validated ENV table. Main34 already has the new mechanism tables.
    This function never commits, changes ENV rows or resets user_version to 33.
    """
    env_present = validate_environment_table(connection, required=version >= 35)
    mechanism = _layout(connection, "object_mechanisms")
    legacy = (("session_id", "TEXT", 1, 1), ("object_id", "TEXT", 1, 2), ("status", "TEXT", 1, 0))
    current = (
        *legacy,
        ("remaining_uses", "INTEGER", 0, 0),
        ("activation_count", "INTEGER", 1, 0),
        ("reset_at_tick", "INTEGER", 0, 0),
        ("resume_status", "TEXT", 0, 0),
    )
    object_keys = {("objects", "session_id", "session_id"), ("objects", "object_id", "object_id")}
    if (
        _foreign_keys(connection, "object_mechanisms") != object_keys
        or connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='old_object_mechanisms'"
        ).fetchone()
    ):
        _unknown_lineage()
    if version == 34 and mechanism == legacy and env_present:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN "
            "('mechanism_activations','mechanism_reset_due','mechanism_actor_frequency')"
        ).fetchone():
            _unknown_lineage()
        for statement in mechanism_statements:
            connection.execute(statement)
        mechanism = _layout(connection, "object_mechanisms")
    if (
        mechanism != current
        or _layout(connection, "mechanism_activations")
        != (
            ("session_id", "TEXT", 1, 1),
            ("activation_id", "TEXT", 1, 2),
            ("object_id", "TEXT", 1, 0),
            ("source_command_id", "TEXT", 1, 0),
            ("source_event_seq", "INTEGER", 1, 0),
            ("actor_id", "TEXT", 0, 0),
            ("operation_id", "TEXT", 1, 0),
        )
        or _foreign_keys(connection, "mechanism_activations")
        != {
            ("object_mechanisms", "session_id", "session_id"),
            ("object_mechanisms", "object_id", "object_id"),
        }
    ):
        _unknown_lineage()
    for index, columns in (
        ("mechanism_reset_due", ("session_id", "reset_at_tick")),
        ("mechanism_actor_frequency", ("session_id", "object_id", "actor_id")),
    ):
        if tuple(r[2] for r in connection.execute(f"PRAGMA index_info({index})")) != columns:
            _unknown_lineage()
