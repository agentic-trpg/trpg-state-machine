"""Public casts of the two reviewed concentration save modifiers."""

import pytest
from dnd5e_srd_data import BundledAssetLoader
from item_support import NPC
from spatial_support import submit as submit_world
from test_session_time_coordination import submit
from test_timed_spell_cast import first_level_balance
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    AdvanceSessionTime,
    DropWorldConcentration,
    StateMachine,
    StateMachineError,
    TypedCommand,
)
from trpg_state_machine.commands import world_command_from_data


def _session(timed_spell_world, spell_id, *, seed=0, npc_cell="2,0"):
    """Give a cleric one exact priced focus and both reviewed spells."""
    canonical = BundledAssetLoader()

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["actors"][0]["initial_cell"] = npc_cell
        if npc_cell == "11,0":
            data["scenes"][0]["grid"]["width"] = 12
            data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"][
                "width"
            ] = 12
        template = data["pc_templates"][0]
        template["character"]["inventory"].append("amulet")
        data["scenes"][0]["objects"].append(
            {
                "object_id": "object.amulet",
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "1,0",
            }
        )
        data["item_transfers"].append(
            {
                "instance_id": "object.amulet",
                "item_slug": "amulet",
                "initial_owner_id": template["pc_template_id"],
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "policy": "ordinary_public",
            }
        )
        fixed = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
        fixed["inventory_state"]["entries"].append(
            {
                "instance_id": "object.amulet",
                "owner_id": fixed["character_states"][0]["entity_id"],
                "item_slug": "amulet",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )

    return timed_spell_world(
        caster_class="cleric",
        caster_subclass="life",
        additional_spells=("bless", "bane"),
        additional_items=(canonical.get_item("amulet"),),
        configure=configure,
        seed=seed,
    )


def _cast(session, spell_id, targets, *, command_id, material=True):
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    command = world_command_from_data(
        {
            "schema_version": "sm-command/1",
            "session_id": sid,
            "command_id": command_id,
            "actor_id": state.pc_actor_id,
            "command_type": "world.cast_spell",
            "expected_world_version": state.world_version,
            "payload": {
                "kind": "world.cast_spell",
                "spell_id": spell_id,
                "spell_source_id": "class:cleric",
                "slot_level": 1,
                "target_id": targets[0],
                "target_ids": list(targets),
                "material_instance_id": "object.amulet"
                if spell_id == "bless" and material
                else None,
            },
        }
    )
    return command, sm.submit_command(command, package, context=context)


def test_public_bless_cast_pays_and_replays(timed_spell_world):
    session = _session(timed_spell_world, "bless", seed=5)
    sm, sid, package, context = session
    with sm:
        assert submit(session, AdvanceSessionTime(11), "ward-start").status == "committed"
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        command, receipt = _cast(session, "bless", (pc, NPC), command_id="cast.bless")
        assert receipt.status == "committed", receipt
        assert first_level_balance(sm, command) == 3
        assert sm.submit_command(command, package, context=context) == receipt
        effects = sm._conn.execute(
            "SELECT target_id,effect_id,concentration_id FROM ongoing_effects "
            "WHERE session_id=? AND cast_command_id=? ORDER BY target_id",
            (sid, command.command_id),
        ).fetchall()
        assert len(effects) == 2
        assert {row["target_id"] for row in effects} == {pc, NPC}
        assert {row["concentration_id"] for row in effects} == {"actor:cast.bless"}
        bane_command, bane_receipt = _cast(
            session, "bane", (NPC, pc), command_id="cast.after-bless"
        )
        assert bane_receipt.status == "committed", bane_receipt
        assert first_level_balance(sm, bane_command) == 2
        saves = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == bane_command.command_id
            and event.payload.get("engine_event", {}).get("type") == "save_rolled"
        ]
        assert [event["target_id"] for event in saves] == [NPC, pc]
        assert all(event["succeeded"] for event in saves)
        assert all(
            any(
                modifier["kind"] == "die" and modifier["value"] > 0
                for modifier in event["roll_detail"]["modifiers"]
            )
            for event in saves
        )
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM ongoing_effects WHERE session_id=? AND cast_command_id=?",
                (sid, command.command_id),
            ).fetchone()[0]
            == 0
        )
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM effect_concentrations WHERE session_id=? AND caster_id=?",
                (sid, pc),
            ).fetchone()[0]
            == 0
        )


def test_public_bane_effect_changes_later_bane_save(timed_spell_world):
    session = _session(timed_spell_world, "bane", seed=1)
    sm, sid, package, context = session
    with sm:
        assert submit(session, AdvanceSessionTime(11), "ward-start").status == "committed"
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        first, receipt = _cast(session, "bane", (pc,), command_id="bane.first")
        assert receipt.status == "committed", receipt
        assert first_level_balance(sm, first) == 3
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM ongoing_effects WHERE session_id=? AND cast_command_id=?",
                (sid, first.command_id),
            ).fetchone()[0]
            == 1
        )
        second, receipt = _cast(session, "bane", (pc,), command_id="bane.second")
        assert receipt.status == "committed", receipt
        assert first_level_balance(sm, second) == 2
        save = next(
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == second.command_id
            and event.payload.get("engine_event", {}).get("type") == "save_rolled"
        )
        assert any(
            modifier["kind"] == "die" and modifier["value"] < 0
            for modifier in save["roll_detail"]["modifiers"]
        )
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM ongoing_effects WHERE session_id=? AND cast_command_id=?",
                (sid, first.command_id),
            ).fetchone()[0]
            == 0
        )


def test_bless_drop_expiry_reopen_and_late_rollback(timed_spell_world, tmp_path):
    session = _session(timed_spell_world, "bless")
    sm, sid, package, context = session
    with sm:
        assert submit(session, AdvanceSessionTime(11), "ward-start").status == "committed"
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        command, _ = _cast(session, "bless", (pc, NPC), command_id="bless.drop")
        assert (
            submit_world(sm, sid, package, context, DropWorldConcentration(), "drop-ward").status
            == "committed"
        )
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM ongoing_effects WHERE session_id=? AND cast_command_id=?",
                (sid, command.command_id),
            ).fetchone()[0]
            == 0
        )
        command, _ = _cast(session, "bless", (pc, NPC), command_id="bless.expire")
        assert submit(session, AdvanceSessionTime(71), "expire-ward").status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()
        current = sm.read_session(sid, package, context=context)
        rollback = TypedCommand(
            sid,
            "bless.rollback",
            pc,
            "world.cast_spell",
            command.payload,
            current.world_version,
        )
        latest = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        balance = first_level_balance(sm, rollback)
        # A late ledger fault must revert payment, group, RNG and events together.
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_ward_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='bless.rollback' "
            "BEGIN SELECT RAISE(ABORT,'injected ward receipt failure'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(rollback, package, context=context)
        assert sm.read_noncombat_snapshot(sid, package, context=context) == latest
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert first_level_balance(sm, rollback) == balance
        sm._conn.execute("DROP TRIGGER fail_ward_receipt")
        loader = sm._rules.loader
    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        assert reopened.submit_command(command, package, context=context).status == "committed"
        assert reopened.read_noncombat_snapshot(sid, package, context=context).effect_states == ()


def test_public_bane_resolves_ordered_saves_once(timed_spell_world):
    session = _session(timed_spell_world, "bane")
    sm, sid, package, context = session
    with sm:
        assert submit(session, AdvanceSessionTime(11), "ward-start").status == "committed"
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        command, receipt = _cast(session, "bane", (NPC, pc), command_id="cast.bane")
        assert receipt.status == "committed", receipt
        events = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == command.command_id and "engine_event" in event.payload
        ]
        saves = [event for event in events if event["type"] == "save_rolled"]
        assert [event["target_id"] for event in saves] == [NPC, pc]
        assert all(event["succeeded"] for event in saves)
        effects = sm._conn.execute(
            "SELECT target_id FROM ongoing_effects WHERE session_id=? AND cast_command_id=?",
            (sid, command.command_id),
        ).fetchall()
        assert {row[0] for row in effects} == {
            event["target_id"] for event in saves if not event["succeeded"]
        }
        assert effects == []


@pytest.mark.parametrize(
    ("spell_id", "npc_cell", "material", "hide", "error"),
    [
        ("bless", "2,0", False, False, "spell.material"),
        ("bless", "11,0", True, False, "spell.target"),
        ("bane", "2,0", True, True, "spell.target"),
    ],
)
def test_ward_preflight_rejects_without_payment_or_rng(
    timed_spell_world, monkeypatch, spell_id, npc_cell, material, hide, error
):
    """A missing priced focus, distant target or unseen Bane target cannot spend."""
    session = _session(timed_spell_world, spell_id, npc_cell=npc_cell)
    sm, sid, package, context = session
    with sm:
        assert submit(session, AdvanceSessionTime(11), "ward-start").status == "committed"
        if hide:
            monkeypatch.setattr(
                "trpg_state_machine.rule_execution.visible_actor_in_range",
                lambda *args, **kwargs: False,
            )
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        command, receipt = _cast(
            session, spell_id, (NPC,), command_id=f"ward.reject.{error}", material=material
        )
        assert receipt.status == "rejected" and receipt.error_code == error
        assert first_level_balance(sm, command) == 4
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
