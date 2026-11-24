"""Explicit single-Session SQLite checkpoints and isolated deliberate loads.

Ordinary save/resume remain reads of the current durable authority. A deliberate
load makes a new database and runtime, so it cannot rewind an active Session.
"""

import os
import random
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from dnd5e_engine.evaluation_rng import RNGState

from .combat import DND_RULESET
from .errors import StateMachineError
from .package import AdventurePackage
from .session_access import _trusted
from .storage import CURRENT_SCHEMA_VERSION


def _database_path(machine) -> Path:
    """Return the absolute path of this runtime's durable SQLite authority."""
    return Path(machine._store._conn.execute("PRAGMA database_list").fetchone()[2]).resolve()


def _fresh_file(path: str | Path, *other_paths: Path) -> Path:
    """Reserve a new destination without following or replacing any existing file."""
    if Path(path).is_symlink():
        raise StateMachineError("checkpoint_exists", "Destination already exists")
    target = Path(path).resolve()
    if target in other_paths:
        raise StateMachineError("invalid_checkpoint", "Checkpoint paths must be distinct")
    if any(
        candidate.exists() or candidate.is_symlink()
        for candidate in (Path(str(target) + "-wal"), Path(str(target) + "-shm"))
    ):
        raise StateMachineError("checkpoint_exists", "Destination SQLite sidecar already exists")
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError as exc:
        raise StateMachineError("checkpoint_exists", "Destination already exists") from exc
    except OSError as exc:
        raise StateMachineError("storage_error", "Cannot create checkpoint destination") from exc
    os.close(fd)
    return target


def _remove_new_database(path: Path) -> None:
    """Remove only files owned by a failed fresh destination operation."""
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)


def _single_session(machine, session_id: str) -> None:
    """Reject whole-file export when it would disclose another Session."""
    sessions = machine._store._conn.execute("SELECT session_id FROM sessions LIMIT 2").fetchall()
    if len(sessions) != 1 or sessions[0][0] != session_id:
        raise StateMachineError(
            "unsupported_checkpoint_scope", "Checkpoint requires a single-Session database"
        )


def create_checkpoint(machine, session_id, package, destination, *, context):
    """Validate an authorized settled Session, then back up one committed SQLite image."""
    machine._ensure_open()
    source = _database_path(machine)
    # Hold the existing in-process commit gate across validation and backup.
    # SQLite backup supplies a consistent image against other connections.
    with machine._store._coordinator.lock:
        machine.save_session(session_id, package, context=context)
        _single_session(machine, session_id)
        pending = getattr(machine, "has_pending_action_choice", lambda _sid: False)
        if pending(session_id):
            raise StateMachineError(
                "pending_action_choice", "Finish or discard the action before checkpointing"
            )
        target = _fresh_file(destination, source)
        try:
            with closing(sqlite3.connect(target)) as output:
                machine._store._conn.backup(output)
        except BaseException:
            _remove_new_database(target)
            raise
    checked = None
    try:
        checked = _runtime_like(machine, target)
        snapshot = checked.save_session(session_id, package, context=context)
        _single_session(checked, session_id)
        checked.close()
        return snapshot
    except BaseException:
        if checked is not None and not checked._store._closed:
            checked.close()
        _remove_new_database(target)
        raise


def _new_rng_state():
    """Create a fresh independent Engine RNG state; tests may replace this entropy hook."""
    return RNGState.capture(random.Random(secrets.randbits(256)))


def _runtime_like(machine, destination):
    """Open a separate runtime using the source runtime's approved rule providers."""
    from .service import StateMachine

    return StateMachine(
        destination,
        evaluator=machine._evaluator,
        dnd_loader=machine._rules.loader,
        environment_effects_provider=machine.environment.effects_provider,
        object_protection_provider=machine._objects.spatial.protection_provider,
        environment_unit_height_provider=machine.environment.unit_height_provider,
    )


def _authorize_checkpoint_source(machine, saved, session_id, package, context):
    """Check the pinned read-only image before copying any bytes to a new path."""
    _trusted(context)
    if not isinstance(package, AdventurePackage):
        raise StateMachineError("invalid_request", "An approved package is required")
    try:
        saved.row_factory = sqlite3.Row
        saved.execute("BEGIN")
        if saved.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
            raise StateMachineError("unsupported_storage_schema", "Checkpoint schema differs")
        if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise StateMachineError("invalid_checkpoint", "Checkpoint integrity check failed")
        rows = saved.execute("SELECT * FROM sessions LIMIT 2").fetchall()
        if not rows or rows[0]["session_id"] != session_id:
            raise StateMachineError("session_not_found", "Unknown checkpoint Session")
        if (rows[0]["principal_id"], rows[0]["player_id"]) != (
            context.principal_id,
            context.player_id,
        ):
            raise StateMachineError("unauthorized", "Context does not control this Session")
        machine._sessions._check_package_pin(rows[0], package)
        if len(rows) != 1:
            raise StateMachineError(
                "unsupported_checkpoint_scope", "Checkpoint requires a single-Session database"
            )
    except sqlite3.Error as exc:
        raise StateMachineError("invalid_checkpoint", "Cannot inspect SQLite checkpoint") from exc


def load_checkpoint(machine, checkpoint, destination, session_id, package, *, context):
    """Copy, validate and reseed an authorized checkpoint in a fresh isolated runtime.

    The checkpoint and active source are never modified. Old receipts remain dated
    evidence; a deliberate new attempt must use a new command ID.
    """
    machine._ensure_open()
    active = _database_path(machine)
    source = Path(checkpoint).resolve()
    if source == active or not source.is_file():
        raise StateMachineError("invalid_checkpoint", "Expected a separate checkpoint file")
    try:
        saved = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise StateMachineError("invalid_checkpoint", "Cannot open SQLite checkpoint") from exc
    target = None
    with closing(saved):
        _authorize_checkpoint_source(machine, saved, session_id, package, context)
        target = _fresh_file(destination, active, source)
        try:
            with closing(sqlite3.connect(target)) as output:
                saved.backup(output)
        except sqlite3.Error as exc:
            _remove_new_database(target)
            raise StateMachineError("invalid_checkpoint", "Cannot copy SQLite checkpoint") from exc
    loaded = None
    try:
        loaded = _runtime_like(machine, target)
        loaded.save_session(session_id, package, context=context)
        _single_session(loaded, session_id)
        row = loaded._store._conn.execute(
            "SELECT ruleset_id FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row[0] != DND_RULESET or package.legacy_audit_only:
            raise StateMachineError(
                "unsupported_checkpoint_scope", "Fresh RNG requires a supported rules Session"
            )
        loaded._session_runtime.rng_context({"session_id": session_id})
        # Stop the runtime before controlled maintenance; reopen from the edited
        # durable image so no old RAM, candidate or cache can survive the load.
        loaded.close()
        loaded = None
        with closing(sqlite3.connect(target)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE rng_states SET state_id=?,position=0,rng_version=0,state_json=? "
                "WHERE session_id=?",
                ("rng." + uuid4().hex, _new_rng_state().model_dump_json(), session_id),
            )
            db.execute(
                "UPDATE sessions SET rng_ref=(SELECT state_id FROM rng_states WHERE session_id=?) "
                "WHERE session_id=?",
                (session_id, session_id),
            )
            db.commit()
        loaded = _runtime_like(machine, target)
        loaded.resume_session(session_id, package, context=context)
        discard = getattr(machine, "discard_pending_action_choice", None)
        if discard is not None:
            discard(session_id)
        return loaded
    except BaseException:
        if loaded is not None:
            loaded.close()
        _remove_new_database(target)
        raise
