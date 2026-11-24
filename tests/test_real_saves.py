"""Public standalone saving throws through the shared rule command path."""

import asyncio
import json
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import SavePayload
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
from dnd5e_srd_data import MemoryAssetLoader
from dnd_support import create, initialize
from item_support import B
from lock_support import CRED, NPC, OTHER_DOOR, A, lock_loader
from modern_support import modern_data, modern_loader
from test_faerie_fire import _grow_remote
from test_lockable_passages import acquire
from test_mechanism_effects import state_rows
from test_mechanism_save_gate import gated_data, grant_bardic
from test_scoped_runtime_validation import measure
from test_world_containers import BOX, container_data
from test_world_mechanisms import LEVER
from world_support import create as create_world
from world_support import setup as setup_world
from world_support import submit as submit_world

from trpg_state_machine import (
    ObjectState,
    PickupItem,
    PutItem,
    SetObjectState,
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


def save_json(sm, sid, package, context, **fields):
    """Read one adjudicated envelope through the public JSON parser."""
    command = save_command(sm, sid, package, context, **fields)
    envelope = asdict(command)
    envelope.pop("check_world_effects")
    envelope["payload"] = command.payload.model_dump(mode="json")
    return world_command_from_data(json.loads(json.dumps(envelope)))


def test_save_json_public_result_failure_replay_and_reopen(
    tmp_path, package_data, context, monkeypatch
):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package = setup_save(tmp_path, package_data, context)
    with sm:
        command = save_json(sm, sid, package, gm, dc=99)
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


@pytest.mark.parametrize(
    ("cover", "expected"), [("half", "pending_choice"), ("total", "committed")]
)
def test_mechanism_source_standalone_save_without_activation(tmp_path, context, cover, expected):
    """The exact source stays inert, including when a held Inspiration could assist a roll."""
    gm = replace(context, can_adjudicate_checks=True)
    data = gated_data(dc=100)
    data["rule_bindings"].pop("combat_foundation")
    next(obj for obj in data["scenes"][0]["objects"] if obj["object_id"] == LEVER)[
        "initial_cell"
    ] = "2,0"
    data["scenes"][0]["grid"]["cover_cells"] = {"1,0": cover}
    data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"][
        "cover_cells"
    ] = {"1,0": cover}
    base = lock_loader()
    loader = MemoryAssetLoader(
        items=[base.get_item(slug) for slug in base.list_slugs("items")],
        monsters=[base.get_monster(slug) for slug in base.list_slugs("monsters")],
        features=[get_default_ruleset().get_feature("bardic-inspiration")],
    )
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    package = _validate(data)
    db = tmp_path / "standalone-ward.db"
    with StateMachine(db, dnd_loader=loader) as sm:
        sid = create_world(sm, package, context)
        actor = sm.read_session(sid, package, context=gm).pc_actor_id
        grant_bardic(sm, sid, actor)
        command = save_json(
            sm,
            sid,
            package,
            gm,
            dc=99,
            source_id=LEVER,
            source_kind="world_object",
            effect_reach={"kind": "direct"},
        )
        before = state_rows(sm)
        rng = sm.read_rng_context(sid, package, context=gm)
        grant = sm._world.characters.effects.held_bardic_inspiration(sid, actor, 0)
        assert grant is not None
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, package, context=context)
        assert denied.value.code == "unauthorized"
        if cover == "total":
            unspecified = save_json(
                sm,
                sid,
                package,
                gm,
                command_id="missing.reach",
                source_id=LEVER,
                source_kind="world_object",
                requires_cover=True,
            )
            rejected = sm.submit_command(unspecified, package, context=gm)
            assert (rejected.status, rejected.error_code) == ("rejected", "save.cover")
            assert sm.read_rng_context(sid, package, context=gm) == rng
            before = state_rows(sm)
            sm._conn.execute(
                "CREATE TEMP TRIGGER fail_no_save_receipt BEFORE INSERT ON command_receipts "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
            with pytest.raises(StateMachineError) as fault:
                sm.submit_command(command, package, context=gm)
            assert fault.value.code == "storage_error"
            assert state_rows(sm) == before
            assert sm.get_command_receipt(sid, command.command_id, package, context=gm) is None
            sm._conn.execute("DROP TRIGGER fail_no_save_receipt")
        receipt = sm.submit_command(command, package, context=gm)
        assert receipt.status == expected, receipt
        assert sm._world.characters.effects.held_bardic_inspiration(sid, actor, 0) == grant
        assert sm.submit_command(command, package, context=gm) == receipt
        after = state_rows(sm)
        for table in ("objects", "mechanism_activations", "object_mechanisms", "characters"):
            assert after[table] == before[table]
        events = sm.read_events(sid, package, context=gm, after_seq=receipt.event_seqs[0] - 1)
        rolls = sm.get_command_rolls(sid, command.command_id, package, context=gm)["rolls"]
        if cover == "total":
            assert len(events) == 1 and events[0].event_type == "rules.save_not_applicable"
            assert events[0].payload["engine_event"] == {
                "type": "save_not_applicable",
                "target_id": actor,
                "source_id": LEVER,
                "source_kind": "world_object",
                "reason": "total_cover",
            }
            assert rolls == []
            assert sm.read_rng_context(sid, package, context=gm) == rng
        else:
            assert rolls[0]["succeeded"] is False
            assert {"kind": "fixed", "source": "cover", "value": 2} in rolls[0]["roll_detail"][
                "modifiers"
            ]
    if cover == "total":
        with StateMachine(db, dnd_loader=loader) as reopened:
            assert reopened.submit_command(command, package, context=gm) == receipt
            assert (
                reopened.read_events(sid, package, context=gm, after_seq=receipt.event_seqs[0] - 1)
                == events
            )


def test_save_source_actor_provenance_verifies_identity_without_geometry(
    tmp_path, package_data, context, monkeypatch
):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package = setup_save(tmp_path, package_data, context)
    with sm:
        monkeypatch.setattr(
            sm._rules, "_save_geometry", lambda *args, **kwargs: pytest.fail("unrequested geometry")
        )
        before = sm.read_rng_context(sid, package, context=gm)
        bad = save_json(sm, sid, package, gm, source_id="npc.missing")
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(bad, package, context=gm)
        assert denied.value.code == "invalid_actor"
        assert sm.read_rng_context(sid, package, context=gm) == before
        valid = save_json(sm, sid, package, gm, source_id=NPC)
        receipt = sm.submit_command(valid, package, context=gm)
        assert receipt.status == "committed", receipt
        event = sm.read_events(sid, package, context=gm, after_seq=receipt.event_seqs[0] - 1)[0]
        assert event.payload["adjudication"]["payload"]["source_id"] == NPC


def test_standalone_save_reads_item_in_closed_container_by_exact_placement(tmp_path, context):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _ = setup_world(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        for operation, key in (
            (PickupItem(BOX, A, "1,0"), "box.pick"),
            (PutItem(A, CRED, BOX, "0,0"), "box.put"),
            (SetObjectState(A, BOX, ObjectState.CLOSED), "box.close"),
        ):
            receipt = submit_world(sm, sid, package, context, operation, key)
            assert receipt.status == "committed", receipt
        actor = sm.read_session(sid, package, context=gm).pc_actor_id
        assert CRED not in {
            entry.instance_id
            for entry in sm.read_carried_items(sid, actor, package, context=context).entries
        }
        before = state_rows(sm)
        for source in (NPC, "object.missing"):
            invalid = save_json(sm, sid, package, gm, source_id=source, source_kind="world_object")
            with pytest.raises(StateMachineError) as denied:
                sm.submit_command(invalid, package, context=gm)
            assert denied.value.code == "invalid_target"
        with sm._transaction(write=True):
            sm.scene_lifecycle.ensure_scene(sid, B, package)
        remote = save_json(
            sm,
            sid,
            package,
            gm,
            source_id=OTHER_DOOR,
            source_kind="world_object",
            effect_reach={"kind": "direct"},
        )
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(remote, package, context=gm)
        assert denied.value.code == "invalid_spatial_state"
        assert state_rows(sm)["rng_states"] == before["rng_states"]
        counts = []
        for epoch, extra in enumerate((0, 25)):
            _grow_remote(sm, sid, extra)
            save = save_json(
                sm,
                sid,
                package,
                gm,
                command_id=f"source.item.{epoch}",
                source_id=CRED,
                source_kind="world_object",
                effect_reach={"kind": "direct"},
            )
            with measure(sm) as (work, _):
                receipt = sm.submit_command(save, package, context=gm)
            assert receipt.status == "committed", receipt
            assert sm.get_command_rolls(sid, save.command_id, package, context=gm)["rolls"]
            counts.append((work["sql_selects"], work["sql_rows"]))
        assert counts[0] == counts[1]
        after = state_rows(sm)
        original_objects = {row[1]: row for row in before["objects"]}
        assert {
            row[1]: row for row in after["objects"] if row[1] in original_objects
        } == original_objects


@pytest.mark.parametrize(("source_cell", "cover_bonus"), [("0,1", 2), ("2,0", 0)])
def test_directional_save_cover_public_replay_and_locality(
    tmp_path, package_data, context, source_cell, cover_bonus
):
    """Only a named current source supplies cover; remote actors do not enter the read."""
    gm = replace(context, can_adjudicate_checks=True)
    counts = []
    for size in (0, 25):
        loader = modern_loader()
        data = modern_data(package_data, loader, spatial=True)
        data["rule_bindings"].pop("combat_foundation")
        data["pc_templates"][0]["initial_cell"] = "2,1"
        data["actors"][0]["initial_cell"] = source_cell
        for scene in data["scenes"]:
            if scene["scene_id"] == "scene.platform":
                scene["grid"]["cover_cells"] = {"1,1": "half"}
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"][
            "cover_cells"
        ] = {"1,1": "half"}
        package = _validate(data)
        db = tmp_path / f"cover-{size}.db"
        sm = StateMachine(db, dnd_loader=loader)
        sid = create(sm, package, context)
        with sm:
            _grow_remote(sm, sid, size)
            covered = save_command(
                sm,
                sid,
                package,
                gm,
                command_id="covered",
                source_id="npc.caretaker",
                requires_cover=True,
            )
            with measure(sm) as (work, _):
                receipt = sm.submit_command(covered, package, context=gm)
            assert receipt.status == "committed"
            counts.append((work["sql_selects"], work["sql_rows"]))
            roll = sm.get_command_rolls(sid, "covered", package, context=gm)["rolls"][0]
            cover_modifiers = [
                m for m in roll["roll_detail"]["modifiers"] if m["source"] == "cover"
            ]
            assert cover_modifiers == (
                [{"kind": "fixed", "source": "cover", "value": cover_bonus}] if cover_bonus else []
            )
            assert sm.submit_command(covered, package, context=gm) == receipt
            plain = save_command(sm, sid, package, gm, command_id="plain")
            assert sm.submit_command(plain, package, context=gm).status == "committed"
            plain_roll = sm.get_command_rolls(sid, "plain", package, context=gm)["rolls"][0]
            assert not any(m["source"] == "cover" for m in plain_roll["roll_detail"]["modifiers"])
            absent = save_command(
                sm,
                sid,
                package,
                gm,
                command_id="absent",
                source_id="npc.attendant",
                requires_cover=True,
            )
            before = sm.read_rng_context(sid, package, context=gm)
            with pytest.raises(StateMachineError) as denied:
                sm.submit_command(absent, package, context=gm)
            assert denied.value.code == "invalid_spatial_state"
            assert sm.read_rng_context(sid, package, context=gm) == before
            if size == 0:
                rollback = save_command(
                    sm,
                    sid,
                    package,
                    gm,
                    command_id="cover.rollback",
                    source_id="npc.caretaker",
                    requires_cover=True,
                )
                sm._conn.execute(
                    "CREATE TEMP TRIGGER fail_cover_receipt BEFORE INSERT ON command_receipts "
                    "BEGIN SELECT RAISE(ABORT, 'injected'); END"
                )
                with pytest.raises(StateMachineError) as fault:
                    sm.submit_command(rollback, package, context=gm)
                assert fault.value.code == "storage_error"
                assert sm.read_rng_context(sid, package, context=gm) == before
                sm._conn.execute("DROP TRIGGER fail_cover_receipt")
                assert sm.submit_command(rollback, package, context=gm).status == "committed"
        with StateMachine(db, dnd_loader=loader) as reopened:
            assert reopened.submit_command(covered, package, context=gm) == receipt
    assert counts[0] == counts[1]
