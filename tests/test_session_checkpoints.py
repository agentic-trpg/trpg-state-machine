"""Public deliberate checkpoint/load boundaries and original authority isolation."""

import random
import sqlite3

import pytest
from dnd5e_engine.evaluation_rng import RNGState
from lock_support import CRED, A, lock_loader
from test_bardic_feature_grant import grant_command, make_bard_world
from test_exploration_healing_potion import exploration_potion_world
from test_mechanism_effects import state_rows
from test_mechanism_save_gate import gated_data
from world_support import command as world_command

from trpg_state_machine import (
    SetLightEnabled,
    StateMachine,
    StateMachineError,
    TrustedContext,
    session_checkpoints,
)


def test_deliberate_load_reseeds_only_fresh_runtime_and_resume_keeps_rng(
    tmp_path, context, monkeypatch
):
    sm, sid, package, loader, db, command = exploration_potion_world(tmp_path, context)
    checkpoint = tmp_path / "before.db"
    with sm:
        before = sm.read_session(sid, package, context=context)
        original_rng = sm.read_rng_context(sid, package, context=context)
        assert sm.create_checkpoint(sid, package, checkpoint, context=context) == before
        committed = sm.submit_command(command, package, context=context)
        assert committed.status == "committed"
        after = sm.read_session(sid, package, context=context)
        advanced_rng = sm.read_rng_context(sid, package, context=context)

        # Cold recovery of the original checkpoint keeps its exact stored stream.
        with StateMachine(checkpoint, dnd_loader=loader) as cold:
            assert cold.resume_session(sid, package, context=context) == before
            assert cold.read_rng_context(sid, package, context=context) == original_rng

        monkeypatch.setattr(
            session_checkpoints,
            "_new_rng_state",
            lambda: RNGState.capture(random.Random(142)),
        )
        discarded = []
        monkeypatch.setattr(
            sm,
            "_discard_pending_action_choice",
            lambda selected: discarded.append(selected),
        )
        loaded = sm.load_checkpoint(
            checkpoint, tmp_path / "loaded.db", sid, package, context=context
        )
        assert discarded == [sid]
        with loaded:
            assert (
                loaded.read_session(sid, package, context=context).world_version
                == before.world_version
            )
            fresh_rng = loaded.read_rng_context(sid, package, context=context)
            assert fresh_rng.stream_id != original_rng.stream_id
            assert fresh_rng.state != original_rng.state
            assert fresh_rng.version == 0
            assert loaded.submit_command(command, package, context=context).status == "committed"
            assert loaded.submit_command(command, package, context=context).status == "committed"

        monkeypatch.setattr(
            session_checkpoints,
            "_new_rng_state",
            lambda: RNGState.capture(random.Random(143)),
        )
        with sm.load_checkpoint(
            checkpoint, tmp_path / "another-load.db", sid, package, context=context
        ) as another:
            another_rng = another.read_rng_context(sid, package, context=context)
            assert another_rng.state != fresh_rng.state
            assert another_rng.stream_id != fresh_rng.stream_id

        assert sm.read_session(sid, package, context=context) == after
        assert sm.read_rng_context(sid, package, context=context) == advanced_rng
        assert (
            sm.get_command_receipt(sid, command.command_id, package, context=context) == committed
        )
        assert db.is_file()


def test_checkpoint_keeps_dated_receipt_on_load(tmp_path, context):
    sm, sid, package, _, _, command = exploration_potion_world(tmp_path, context)
    with sm:
        receipt = sm.submit_command(command, package, context=context)
        checkpoint = tmp_path / "after.db"
        sm.create_checkpoint(sid, package, checkpoint, context=context)
        loaded = sm.load_checkpoint(
            checkpoint, tmp_path / "replay.db", sid, package, context=context
        )
        with loaded:
            assert (
                loaded.get_command_receipt(sid, command.command_id, package, context=context)
                == receipt
            )
            assert loaded.submit_command(command, package, context=context) == receipt


def test_checkpoint_refuses_auth_pending_existing_and_invalid_input(tmp_path, context, monkeypatch):
    sm, sid, package, _, _, _ = exploration_potion_world(tmp_path, context)
    checkpoint = tmp_path / "authorized.db"
    wrong = TrustedContext("authenticated:other", context.player_id, ())
    with sm:
        with pytest.raises(StateMachineError) as auth:
            sm.create_checkpoint(sid, package, checkpoint, context=wrong)
        assert auth.value.code == "unauthorized"
        assert not checkpoint.exists()

        monkeypatch.setattr(sm, "_has_pending_action_choice", lambda _sid: True)
        with pytest.raises(StateMachineError) as pending:
            sm.create_checkpoint(sid, package, checkpoint, context=context)
        assert pending.value.code == "pending_action_choice"
        assert not checkpoint.exists()
        monkeypatch.setattr(sm, "_has_pending_action_choice", lambda _sid: False)

        sm.create_checkpoint(sid, package, checkpoint, context=context)
        with pytest.raises(StateMachineError) as exists:
            sm.create_checkpoint(sid, package, checkpoint, context=context)
        assert exists.value.code == "checkpoint_exists"

        destination = tmp_path / "unauthorized-load.db"
        discarded = []
        monkeypatch.setattr(
            sm,
            "_discard_pending_action_choice",
            lambda selected: discarded.append(selected),
        )
        with pytest.raises(StateMachineError) as auth:
            sm.load_checkpoint(checkpoint, destination, sid, package, context=wrong)
        assert auth.value.code == "unauthorized"
        assert not destination.exists()
        assert discarded == []

        sidecar = tmp_path / "blocked.db-wal"
        sidecar.write_text("unrelated")
        with pytest.raises(StateMachineError) as exists:
            sm.load_checkpoint(checkpoint, tmp_path / "blocked.db", sid, package, context=context)
        assert exists.value.code == "checkpoint_exists"
        assert sidecar.read_text() == "unrelated"
        assert not (tmp_path / "blocked.db").exists()

        malformed = tmp_path / "malformed.db"
        malformed.write_text("not a SQLite database")
        with pytest.raises(StateMachineError):
            sm.load_checkpoint(
                malformed, tmp_path / "invalid-load.db", sid, package, context=context
            )
        assert not (tmp_path / "invalid-load.db").exists()
        old_schema = tmp_path / "old-schema.db"
        with sqlite3.connect(checkpoint) as source, sqlite3.connect(old_schema) as target:
            source.backup(target)
            target.execute("PRAGMA user_version=1")
        with pytest.raises(StateMachineError) as schema:
            sm.load_checkpoint(old_schema, tmp_path / "old-load.db", sid, package, context=context)
        assert schema.value.code == "unsupported_storage_schema"
        assert not (tmp_path / "old-load.db").exists()
        assert discarded == []
        with sqlite3.connect(checkpoint) as disk:
            assert disk.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_checkpoint_refuses_whole_database_with_another_session(
    tmp_path, package, request_for, context
):
    with StateMachine(tmp_path / "two.db") as sm:
        first = sm.create_session(request_for("create.first"), package, context=context)
        sm.create_session(request_for("create.second"), package, context=context)
        target = tmp_path / "disallowed.db"
        with pytest.raises(StateMachineError) as scope:
            sm.create_checkpoint(first.session_id, package, target, context=context)
        assert scope.value.code == "unsupported_checkpoint_scope"
        assert not target.exists()


def test_loading_pre_action_checkpoint_discards_live_bardic_save_and_changes_roll(
    tmp_path, context, monkeypatch
):
    """A real ward rolls from the freshly loaded stream after discarding its prior attempt."""
    import test_bardic_feature_grant as bard_fixture

    monkeypatch.setattr(bard_fixture, "item_data", lambda: gated_data(dc=100))
    monkeypatch.setattr(bard_fixture, "item_loader", lock_loader)
    db, sid, package, loader, context = make_bard_world(tmp_path, context)
    checkpoint = tmp_path / "before-ward.db"
    operation = SetLightEnabled(A, CRED, "0,0", True)
    with StateMachine(db, dnd_loader=loader) as machine:
        grant = grant_command(machine, sid, package, context)
        assert machine.submit_command(grant, package, context=context).status == "committed"
        machine.create_checkpoint(sid, package, checkpoint, context=context)
        committed = state_rows(machine)
        original_rng = machine.read_rng_context(sid, package, context=context)
        first = world_command(machine, sid, package, context, operation, "first-ward")
        assert machine.submit_command(first, package, context=context).status == "pending_choice"
        rolls = machine.get_command_rolls(sid, first.command_id, package, context=context)["rolls"]
        original_die = rolls[0]["natural"]
        assert state_rows(machine) == committed

        with pytest.raises(StateMachineError, match="Finish or discard") as pending:
            machine.create_checkpoint(sid, package, tmp_path / "mid-ward.db", context=context)
        assert pending.value.code == "pending_action_choice"
        seed = next(seed for seed in (4, 2) if random.Random(seed).randint(1, 20) != original_die)
        fresh_state = RNGState.capture(random.Random(seed))
        monkeypatch.setattr(session_checkpoints, "_new_rng_state", lambda: fresh_state)
        with machine.load_checkpoint(
            checkpoint, tmp_path / "fresh-ward.db", sid, package, context=context
        ) as loaded:
            assert not machine._has_pending_action_choice(sid)
            assert state_rows(machine) == committed
            assert machine.read_rng_context(sid, package, context=context) == original_rng
            assert loaded.read_rng_context(sid, package, context=context).state == fresh_state

            second = world_command(loaded, sid, package, context, operation, "second-ward")
            assert (
                loaded.submit_command(second, package, context=context).status == "pending_choice"
            )
            fresh_rolls = loaded.get_command_rolls(
                sid, second.command_id, package, context=context
            )["rolls"]
            assert fresh_rolls[0]["natural"] != original_die
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert not reopened._has_pending_action_choice(sid)
        assert reopened.read_rng_context(sid, package, context=context) == original_rng
