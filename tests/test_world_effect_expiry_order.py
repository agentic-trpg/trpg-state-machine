"""Ordered actor effect settlement through the public Session clock."""

import pytest
from item_support import NPC
from spatial_support import command, submit
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import AdvanceSessionTime, StateMachine, StateMachineError


def _druid_world(timed_spell_world, database):
    """Bind two canonical actor spells with equal world duration."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")

    return timed_spell_world(
        database=database,
        caster_class="druid",
        caster_subclass="land",
        additional_spells=("barkskin",),
        configure=configure,
    )


def _cast(session, spell_id, command_id, *, target=None, level=1):
    """Use the authorized exploration spell command with its current version."""
    sm, _, package, context = session
    cast = cast_command(
        session,
        spell_id=spell_id,
        source_id="class:druid",
        target_id=target,
        slot_level=level,
        command_id=command_id,
    )
    receipt = sm.submit_command(cast, package, context=context)
    assert receipt.status == "committed", receipt
    return cast.actor_id


def test_world_expiry_preserves_actor_effect_list_order_after_rollback_and_reopen(
    timed_spell_world, tmp_path
):
    session = _druid_world(timed_spell_world, "ordered.sqlite")
    sm, sid, package, context = session
    with sm:
        pc = _cast(session, "longstrider", "earlier-deadline")
        assert submit(sm, sid, package, context, AdvanceSessionTime(1), "later-start").status == (
            "committed"
        )
        _cast(session, "longstrider", "first-in-list")
        _cast(session, "barkskin", "second-in-list", level=2)
        _cast(session, "longstrider", "other-actor", target=NPC)

        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        earlier, first, second, other = snapshot.effect_states
        assert earlier.target_id == first.target_id == second.target_id == pc
        assert other.target_id == NPC < pc
        assert second.id < first.id
        old_events = sm.read_events(sid, package, context=context)
        advance = command(sm, sid, package, context, AdvanceSessionTime(3602), "settle-effects")
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_expiry_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='settle-effects' AND NOT EXISTS ("
            "SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id) "
            "BEGIN SELECT RAISE(ABORT,'injected failure after all expiries'); END"
        )
        with pytest.raises(StateMachineError) as fault:
            sm.submit_command(advance, package, context=context)
        assert fault.value.code == "storage_error"
        assert str(fault.value.__cause__) == "injected failure after all expiries"
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 1
        assert sm.read_noncombat_snapshot(sid, package, context=context) == snapshot
        assert sm.read_events(sid, package, context=context) == old_events
        assert sm.get_command_receipt(sid, "settle-effects", package, context=context) is None
        loader = sm._rules.loader

    with StateMachine(tmp_path / "ordered.sqlite", dnd_loader=loader) as sm:
        assert sm.read_noncombat_snapshot(sid, package, context=context) == snapshot
        receipt = sm.submit_command(advance, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 3602
        assert not sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        events = sm.read_events(sid, package, context=context)
        expiries = [event for event in events if event.event_type == "effect.expired"]
        assert all(event.event_seq in receipt.event_seqs for event in expiries)
        assert [event.payload["boundary_tick"] for event in expiries] == [3600, 3601, 3601, 3601]
        assert [tuple(event.payload["identity"]) for event in expiries] == [
            (effect.target_id, effect.id, effect.origin)
            for effect in (earlier, other, first, second)
        ]

    with StateMachine(tmp_path / "ordered.sqlite", dnd_loader=loader) as sm:
        assert sm.submit_command(advance, package, context=context) == receipt
        assert sm.read_events(sid, package, context=context) == events
        assert not sm.read_noncombat_snapshot(sid, package, context=context).effect_states


def test_world_expiry_selection_ignores_unrelated_future_and_untimed_effects(timed_spell_world):
    def measure(unrelated_count):
        session = _druid_world(timed_spell_world, f"expiry-scale-{unrelated_count}.sqlite")
        sm, sid, package, context = session
        with sm:
            _cast(session, "longstrider", "first-in-list")
            _cast(session, "barkskin", "second-in-list", level=2)
            first, second = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
            assert second.id < first.id
            unrelated = first.model_dump(mode="json")
            with sm._transaction(write=True):
                for index in range(unrelated_count):
                    sm._characters.effects.add_effect(
                        sid,
                        {**unrelated, "target_id": NPC, "origin": f"unrelated.{index}"},
                        expires_tick=7200 if index % 2 else None,
                    )

            deadlines = []
            deadline_steps = 0
            reading_deadline = False

            def trace(sql):
                nonlocal reading_deadline
                reading_deadline = (
                    sql.startswith(
                        (
                            "SELECT target_id,effect_id,origin,expires_tick,payload_json ",
                            "SELECT min(expires_tick) ",
                        )
                    )
                    and "FROM ongoing_effects " in sql
                )
                if reading_deadline:
                    deadlines.append(sql)

            def progress():
                nonlocal deadline_steps
                deadline_steps += reading_deadline

            advance = command(sm, sid, package, context, AdvanceSessionTime(3600), "settle-two")
            sm._conn.set_trace_callback(trace)
            sm._conn.set_progress_handler(progress, 1)
            try:
                receipt = sm.submit_command(advance, package, context=context)
            finally:
                sm._conn.set_progress_handler(None, 0)
                sm._conn.set_trace_callback(None)
            assert receipt.status == "committed", receipt
            for sql in set(deadlines):
                (row,) = sm._conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
                assert row[3].startswith("SEARCH ongoing_effects USING ")
                assert "INDEX ongoing_effects_world_deadline" in row[3]

            expiries = [
                event
                for event in sm.read_events(sid, package, context=context)
                if event.event_type == "effect.expired"
            ]
            assert [event.payload["identity"][1] for event in expiries] == [first.id, second.id]
            (leftovers,) = sm._conn.execute(
                "SELECT count(*) FROM ongoing_effects WHERE session_id=? AND target_id=?",
                (sid, NPC),
            ).fetchone()
            assert leftovers == unrelated_count
            assert deadlines and deadline_steps
            return len(deadlines), deadline_steps

    # Both worlds have a later deadline after the two target effects are removed.
    assert measure(2) == measure(258)
