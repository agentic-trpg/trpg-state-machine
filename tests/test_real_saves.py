"""Public standalone saving throws through the shared rule command path."""

import asyncio
import json
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import SavePayload
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_srd_data import MemoryAssetLoader
from dnd_support import create, initialize
from item_support import B
from modern_support import modern_data, modern_loader
from test_faerie_fire import _grow_remote
from test_scoped_runtime_validation import measure

from trpg_state_machine import (
    StateMachine,
    StateMachineError,
    TypedCommand,
    world_command_from_data,
)
from trpg_state_machine.package import _validate


def setup_save(tmp_path, package_data, context, *, condition=None):
    """Create a fresh current-schema session with a real noncombat character."""
    loader = modern_loader()
    if condition:
        base = loader
        loader = MemoryAssetLoader(
            items=[base.get_item(slug) for slug in base.list_slugs("items")],
            monsters=[base.get_monster(slug) for slug in base.list_slugs("monsters")],
            conditions=[get_default_ruleset().get_condition(condition)],
        )
    package = _validate(modern_data(package_data, loader))
    sm = StateMachine(tmp_path / "saves.db", dnd_loader=loader)
    sid = create(sm, package, context)
    if condition:
        actor_id = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._store.transaction(write=True):
            sm._characters.replace_conditions(
                sid,
                actor_id,
                [
                    dict(
                        condition=condition,
                        source_entity_id="implied:condition",
                        scope="session",
                        duration_rounds=None,
                        save_dc=None,
                        applied_round=0,
                        exhaustion_level=1,
                        source_effect_id=None,
                    )
                ],
            )
    return sm, sid, package


def save_command(sm, sid, package, context, *, command_id="save.1", **fields):
    """Bind an explicit adjudication to the selected saving actor."""
    session = sm.read_session(sid, package, context=context)
    data = dict(
        kind="rules.save",
        target_id=session.pc_actor_id,
        ability="dex",
        dc=10,
        advantage=(),
        disadvantage=(),
        is_magical=False,
    )
    data.update(fields)
    return TypedCommand(
        sid,
        command_id,
        session.pc_actor_id,
        "rules.save",
        SavePayload(**data),
        session.world_version,
    )


def test_save_json_public_result_failure_replay_and_reopen(
    tmp_path, package_data, context, monkeypatch
):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package = setup_save(tmp_path, package_data, context)
    with sm:
        command = save_command(sm, sid, package, gm, dc=99)
        envelope = asdict(command)
        envelope.pop("check_world_effects")
        command = world_command_from_data(
            json.loads(json.dumps({**envelope, "payload": command.payload.model_dump(mode="json")}))
        )
        real = sm._rules._evaluate_dnd
        rule_actors = sm._rules.characters.rule_actors
        owners_read = []

        def exact_actors(sid, owners):
            owners_read.append(frozenset(owners))
            return rule_actors(sid, owners)

        monkeypatch.setattr(sm._rules.characters, "rule_actors", exact_actors)
        monkeypatch.setattr(
            sm._rules.world.objects.lights,
            "current_project",
            lambda *args, **kwargs: pytest.fail("save has no lighting dependency"),
        )
        requests = []

        async def capture(request):
            requests.append(request)
            assert not sm._conn.in_transaction
            assert request.operation_kind == "rules.save"
            assert [a.entity_id for a in request.state_snapshot.character_states] == [
                command.actor_id
            ]
            return await real(request)

        sm._rules._evaluate_dnd = capture
        receipt = asyncio.run(sm.submit_command_async(command, package, context=gm))
        assert receipt.status == "committed" and len(requests) == 1
        assert owners_read and set(owners_read) == {frozenset((command.actor_id,))}
        event = sm.read_events(sid, package, context=gm)[-1]
        assert event.event_type == "rules.save_rolled"
        assert event.payload["engine_event"]["type"] == "save_rolled"
        assert event.payload["engine_event"]["succeeded"] is False
        assert event.payload["adjudication"]["payload"] == command.payload.model_dump(mode="json")
        assert sm.get_command_rolls(sid, command.command_id, package, context=gm)["rolls"]
        assert sm.submit_command(command, package, context=gm) == receipt
        assert len(requests) == 1
        assert json.loads(command.payload.model_dump_json())["kind"] == "rules.save"
    with StateMachine(tmp_path / "saves.db", dnd_loader=modern_loader()) as reopened:
        assert reopened.submit_command(command, package, context=gm) == receipt


def test_save_authority_and_identity_reject_without_draw(tmp_path, package_data, context):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package = setup_save(tmp_path, package_data, context)
    with sm:
        command = save_command(sm, sid, package, gm)
        rng = sm.read_rng_context(sid, package, context=gm)
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, package, context=context)
        assert denied.value.code == "unauthorized"
        wrong = replace(
            command, payload=command.payload.model_copy(update={"target_id": "npc.other"})
        )
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(wrong, package, context=gm)
        assert denied.value.code == "invalid_actor"
        assert sm.read_rng_context(sid, package, context=gm) == rng


def test_paralyzed_dex_auto_failure_consumes_no_rng(tmp_path, package_data, context):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package = setup_save(tmp_path, package_data, context, condition="paralyzed")
    with sm:
        command = save_command(sm, sid, package, gm, ability="dex")
        before = sm.read_rng_context(sid, package, context=gm)
        receipt = sm.submit_command(command, package, context=gm)
        assert receipt.status == "committed"
        roll = sm.get_command_rolls(sid, command.command_id, package, context=gm)["rolls"][0]
        assert roll["roll_detail"]["status"] == "automatic_failure"
        assert roll["natural"] is None and roll["succeeded"] is False
        assert sm.read_rng_context(sid, package, context=gm).state == before.state


def test_restrained_dex_and_wis_use_distinct_rule_modifiers(tmp_path, package_data, context):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package = setup_save(tmp_path, package_data, context, condition="restrained")
    with sm:
        dex = save_command(sm, sid, package, gm, command_id="save.dex", ability="dex")
        assert sm.submit_command(dex, package, context=gm).status == "committed"
        wis = save_command(sm, sid, package, gm, command_id="save.wis", ability="wis")
        assert sm.submit_command(wis, package, context=gm).status == "committed"
        dex_roll = sm.get_command_rolls(sid, "save.dex", package, context=gm)["rolls"][0]
        wis_roll = sm.get_command_rolls(sid, "save.wis", package, context=gm)["rolls"][0]
        assert dex_roll["advantage"] == "disadvantage"
        assert wis_roll["advantage"] == "normal"


def test_active_combat_blocks_new_save_but_keeps_prior_replay(tmp_path, package_data, context):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package = setup_save(tmp_path, package_data, context)
    with sm:
        original = save_command(sm, sid, package, gm)
        receipt = sm.submit_command(original, package, context=gm)
        assert initialize(sm, sid, package, context=gm).status == "committed"
        assert sm.submit_command(original, package, context=gm) == receipt
        current = save_command(sm, sid, package, gm, command_id="save.combat")
        before = sm.read_rng_context(sid, package, context=gm)
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(current, package, context=gm)
        assert denied.value.code == "unsupported_operation"
        assert sm.read_rng_context(sid, package, context=gm) == before


def test_save_receipt_fault_rolls_back_and_retry(tmp_path, package_data, context):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package = setup_save(tmp_path, package_data, context)
    with sm:
        command = save_command(sm, sid, package, gm)
        before = sm.read_rng_context(sid, package, context=gm)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_save_receipt BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(StateMachineError) as fault:
            sm.submit_command(command, package, context=gm)
        assert fault.value.code == "storage_error"
        assert sm.read_rng_context(sid, package, context=gm) == before
        assert sm.get_command_receipt(sid, command.command_id, package, context=gm) is None
        sm._conn.execute("DROP TRIGGER fail_save_receipt")
        assert sm.submit_command(command, package, context=gm).status == "committed"


def test_save_actor_reads_stay_fixed_as_remote_state_grows(tmp_path, package_data, context):
    gm = replace(context, can_adjudicate_checks=True)
    counts = []
    for size in (0, 25):
        directory = tmp_path / str(size)
        directory.mkdir()
        sm, sid, package = setup_save(directory, package_data, context)
        with sm:
            with sm._store.transaction(write=True):
                sm.scene_lifecycle.ensure_scene(sid, B, package)
            _grow_remote(sm, sid, size)
            command = save_command(sm, sid, package, gm)
            with measure(sm) as (work, _):
                receipt = sm.submit_command(command, package, context=gm)
            assert receipt.status == "committed", receipt
            counts.append((work["sql_selects"], work["sql_rows"]))
    assert counts[0] == counts[1]
