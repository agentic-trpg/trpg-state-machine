"""Public controlled-Bard grant and the existing paid check continuation."""

import asyncio
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.build_spec import derive_multiclass_slots, proficiency_grants
from dnd5e_engine.evaluation_bardic_grant import ExplorationFeaturePayload
from dnd5e_engine.evaluation_contracts import SavePayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
from dnd5e_srd_data import MemoryAssetLoader
from dnd_support import create
from item_support import NPC, B, item_data, item_loader
from test_faerie_fire import _grow_remote
from test_real_checks import check_command
from test_scoped_runtime_validation import measure

from trpg_state_machine import AdvanceSessionTime, StateMachine, TimeBinding, TypedCommand
from trpg_state_machine.choice_models import ChoiceRespondOperationPayload
from trpg_state_machine.commands import world_command_from_data
from trpg_state_machine.errors import StateMachineError
from trpg_state_machine.package import _validate


def make_bard_world(tmp_path, context, *, uses=3, hidden=False, npc_cell="2,0"):
    """Prepare two spatial actors and a canonical Bard 5 resource owner."""
    canonical = get_default_ruleset()
    base = item_loader()
    bard = canonical.get_class("bard")
    lore = canonical.get_subclass("lore")
    features = sorted(
        {
            grant.slug
            for source in (bard, lore)
            for grant in source.granted_features
            if grant.level <= 5 and grant.ref_type == "feature"
        }
    )
    loader = MemoryAssetLoader(
        classes=[bard],
        subclasses=[lore],
        features=[canonical.get_feature(slug) for slug in features],
        conditions=[canonical.get_condition(slug) for slug in ("paralyzed", "restrained")],
        items=list(base._items.values()),
        monsters=list(base._monsters.values()),
    )
    data = item_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    data["time_binding"] = asdict(TimeBinding())
    data["actors"][0]["senses"] = {"visual": True, "auditory": True}
    data["actors"][0].pop("npc_state", None)
    data["actors"][0]["initial_cell"] = npc_cell
    for scene in data["scenes"]:
        if "grid" in scene:
            scene["grid"]["width"] = 20
    data["pc_templates"][0]["character"]["inventory"] = []
    slots = derive_multiclass_slots({"bard": 5}, loader=loader)
    proficiency = proficiency_grants([(bard, 5, "primary")])
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/20"
        snapshot["scene_state"]["grid"]["width"] = 20
        for scene in foundation.get("additional_scenes", ()):
            scene["grid"]["width"] = 20
        if "combat_state" in snapshot:
            snapshot["combat_state"]["actor_zone"][NPC] = npc_cell
        pc, npc = snapshot["character_states"]
        pc.update(
            carried_item_slugs=[],
            weapon_in_hands=None,
            weapon_grip="none",
        )
        npc.update(
            class_slug="bard",
            classes={"bard": 5},
            subclass_slug="lore",
            granted_features=features,
            character_level=5,
            charisma=16,
            hp_current=20,
            hp_max=20,
            proficiency_bonus_override=None,
            save_proficiencies=sorted(proficiency.saves),
            weapon_proficiencies=sorted(proficiency.weapons),
            spell_slots=slots,
            custom_counters={"feature_use:bardic-inspiration": {"spent": 3 - uses}},
        )
        for actor in (pc, npc):
            actor.pop("spells_known", None)
        snapshot["inventory_state"]["entries"] = [
            entry
            for entry in snapshot["inventory_state"]["entries"]
            if entry["owner_id"] != pc["entity_id"]
        ]
        snapshot["resource_state"] = {
            "pools": [
                {
                    "kind": "spell_slot",
                    "owner_id": NPC,
                    "pool_id": f"spell:{level}",
                    "current": value,
                    "maximum": value,
                    "level": level,
                }
                for level, value in slots.items()
            ]
            + [
                {
                    "kind": "feature_uses",
                    "owner_id": NPC,
                    "pool_id": "bardic-inspiration",
                    "current": uses,
                    "maximum": 3,
                    "feature_slug": "bardic-inspiration",
                }
            ]
        }
    package = _validate(data)
    context = replace(context, controlled_npc_ids=(NPC,), can_adjudicate_checks=True)
    db = tmp_path / "bardic-grant.sqlite"
    with StateMachine(db, dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        if hidden:
            with sm._store.transaction(write=True):
                sm._world.characters.add_condition(
                    sid,
                    NPC,
                    {
                        "condition": "invisible",
                        "source_entity_id": "implied:test",
                        "scope": "session",
                    },
                )
    return db, sid, package, loader, context


@pytest.fixture
def bard_world(tmp_path, context):
    """Provide the default visible, funded controlled Bard scenario."""
    return make_bard_world(tmp_path, context)


def grant_command(sm, sid, package, context, key="grant"):
    """Build the trusted NPC's public feature command at the current version."""
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        NPC,
        "world.use_feature",
        ExplorationFeaturePayload(
            kind="world.use_feature",
            feature_id="bardic-inspiration",
            target_id=state.pc_actor_id,
        ),
        state.world_version,
    )


def test_controlled_bard_grants_die_then_pc_spends_it(bard_world):
    db, sid, package, loader, context = bard_world
    with StateMachine(db, dnd_loader=loader) as sm:
        command = grant_command(sm, sid, package, context)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        pool = next(p for p in after.resource_state.pools if p.pool_id == "bardic-inspiration")
        assert pool.current == 2
        assert len(after.effect_states) == 1
        effect = after.effect_states[0]
        assert (effect.id, effect.origin, effect.target_id) == (
            "effect:inspired",
            f"cast:inspired:{NPC}",
            command.payload.target_id,
        )
        assert effect.flags["bardic_inspiration"] == {"grant_id": "grant", "die_sides": 8}
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_noncombat_snapshot(sid, package, context=context) == after
        duplicate = grant_command(sm, sid, package, context, "duplicate")
        assert sm.submit_command(duplicate, package, context=context).status == "rejected"
        check = check_command(sm, sid, package, context, key="check-with-grant", dc=99)
        pending = sm.submit_command(check, package, context=context)
        assert pending.status == "pending_choice", pending
        state = sm.read_session(sid, package, context=context)
        answer = TypedCommand(
            sid,
            "accept-grant",
            state.pc_actor_id,
            "choice.respond",
            ChoiceRespondOperationPayload(check.command_id, "accept"),
            state.world_version,
        )
        assert sm.submit_command(answer, package, context=context).status == "committed"
        assert not sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        assert (
            next(
                p for p in before.resource_state.pools if p.pool_id == "bardic-inspiration"
            ).current
            == 3
        )


@pytest.mark.parametrize("option", ["accept", "decline"])
def test_controlled_bard_grant_continues_standalone_save(bard_world, option):
    """Use the real public grant, save and response entries with fixed original evidence."""
    db, sid, package, loader, context = bard_world
    with StateMachine(db, dnd_loader=loader) as sm:
        assert (
            sm.submit_command(
                grant_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        state = sm.read_session(sid, package, context=context)
        current_rng = sm.read_rng_context(sid, package, context=context)
        expected = current_rng.state.restore()
        natural = expected.randint(1, 20)
        unassisted_total = natural + 4  # The fixture has Dexterity 18.
        source = TypedCommand(
            sid,
            "save-with-grant",
            state.pc_actor_id,
            "rules.save",
            SavePayload(
                kind="rules.save",
                target_id=state.pc_actor_id,
                ability="dex",
                dc=unassisted_total + 1,
                advantage=(),
                disadvantage=(),
                is_magical=False,
            ),
            state.world_version,
        )
        pending = sm.submit_command(source, package, context=context)
        assert pending.status == "pending_choice", pending
        original = sm.get_command_rolls(sid, source.command_id, package, context=context)
        assert original and original["rolls"][0]["type"] == "save_rolled"
        assert original["rolls"][0]["natural"] == natural
        assert original["rolls"][0]["succeeded"] is False
        state = sm.read_session(sid, package, context=context)
        answer = TypedCommand(
            sid,
            f"{option}-save",
            state.pc_actor_id,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, option),
            state.world_version,
        )
        receipt = sm.submit_command(answer, package, context=context)
        assert receipt.status == "committed", receipt
        (roll,) = sm.get_command_rolls(sid, answer.command_id, package, context=context)["rolls"]
        bonus = expected.randint(1, 8) if option == "accept" else 0
        assert roll["natural"] == natural
        assert roll["roll_total"] == unassisted_total + bonus
        assert roll["succeeded"] is (option == "accept")
        assert sm.submit_command(answer, package, context=context) == receipt
        effects = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        assert bool(effects) is (option == "decline")


def test_bardic_save_uses_current_rng_after_other_roll_and_reopen(bard_world):
    """An unrelated roll can advance RNG while the save's d20 remains fixed."""
    db, sid, package, loader, context = bard_world
    with StateMachine(db, dnd_loader=loader) as sm:
        assert (
            sm.submit_command(
                grant_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        state = sm.read_session(sid, package, context=context)
        source = TypedCommand(
            sid,
            "save-pending",
            state.pc_actor_id,
            "rules.save",
            SavePayload(
                kind="rules.save",
                target_id=state.pc_actor_id,
                ability="dex",
                dc=99,
                advantage=(),
                disadvantage=(),
                is_magical=False,
            ),
            state.world_version,
        )
        assert sm.submit_command(source, package, context=context).status == "pending_choice"
        original = sm.get_command_rolls(sid, source.command_id, package, context=context)["rolls"][
            0
        ]
        other = check_command(sm, sid, package, context, key="unrelated-check", dc=1)
        assert sm.submit_command(other, package, context=context).status == "committed"
        rng = sm.read_rng_context(sid, package, context=context)
    with StateMachine(db, dnd_loader=loader) as sm:
        sm.resume_session(sid, package, context=context)
        state = sm.read_session(sid, package, context=context)
        answer = TypedCommand(
            sid,
            "answer-pending",
            state.pc_actor_id,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, "accept"),
            state.world_version,
        )
        assert sm.submit_command(answer, package, context=context).status == "committed"
        final = sm.get_command_rolls(sid, answer.command_id, package, context=context)["rolls"][0]
        stream = rng.state.restore()
        face = stream.randint(1, 8)
        assert final["roll_detail"]["d20"] == original["roll_detail"]["d20"]
        assert final["roll_total"] == original["roll_total"] + face
        assert final["roll_detail"]["modifiers"][-1]["source"] == "feature_grant:bardic-inspiration"
        assert sm.read_rng_context(sid, package, context=context).state == RNGState.capture(stream)


@pytest.mark.parametrize(
    ("condition", "status"), [("restrained", "pending_choice"), ("paralyzed", "committed")]
)
def test_bardic_save_condition_change_and_auto_failure(bard_world, condition, status):
    """A selected save dependency stales a window; auto-failure opens none."""
    db, sid, package, loader, context = bard_world
    with StateMachine(db, dnd_loader=loader) as sm:
        assert (
            sm.submit_command(
                grant_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        state = sm.read_session(sid, package, context=context)
        if condition == "paralyzed":
            with sm._store.transaction(write=True):
                sm._world.characters.add_condition(
                    sid,
                    state.pc_actor_id,
                    {
                        "condition": condition,
                        "source_entity_id": "implied:test",
                        "scope": "session",
                    },
                )
        source = TypedCommand(
            sid,
            "save-condition",
            state.pc_actor_id,
            "rules.save",
            SavePayload(
                kind="rules.save",
                target_id=state.pc_actor_id,
                ability="dex",
                dc=99,
                advantage=(),
                disadvantage=(),
                is_magical=False,
            ),
            state.world_version,
        )
        before = sm.read_rng_context(sid, package, context=context)
        opened = sm.submit_command(source, package, context=context)
        assert opened.status == status, opened
        if condition == "paralyzed":
            assert sm.read_rng_context(sid, package, context=context) == before
            return
        with sm._store.transaction(write=True):
            sm._world.characters.add_condition(
                sid,
                state.pc_actor_id,
                {"condition": condition, "source_entity_id": "implied:test", "scope": "session"},
            )
        rng = sm.read_rng_context(sid, package, context=context)
        latest = sm.read_events(sid, package, context=context)
        state = sm.read_session(sid, package, context=context)
        answer = TypedCommand(
            sid,
            "answer-stale",
            state.pc_actor_id,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, "accept"),
            state.world_version,
        )
        rejected = sm.submit_command(answer, package, context=context)
        assert (rejected.status, rejected.error_code) == ("rejected", "choice_stale")
        assert sm.read_events(sid, package, context=context) == latest
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_bardic_save_reply_late_receipt_failure_rolls_back(bard_world, monkeypatch):
    """A late receipt error restores the grant, RNG, event and pending choice."""
    db, sid, package, loader, context = bard_world
    with StateMachine(db, dnd_loader=loader) as sm:
        assert (
            sm.submit_command(
                grant_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        state = sm.read_session(sid, package, context=context)
        source = TypedCommand(
            sid,
            "save-fault",
            state.pc_actor_id,
            "rules.save",
            SavePayload(
                kind="rules.save",
                target_id=state.pc_actor_id,
                ability="dex",
                dc=99,
                advantage=(),
                disadvantage=(),
                is_magical=False,
            ),
            state.world_version,
        )
        assert sm.submit_command(source, package, context=context).status == "pending_choice"
        state = sm.read_session(sid, package, context=context)
        answer = TypedCommand(
            sid,
            "answer-fault",
            state.pc_actor_id,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, "accept"),
            state.world_version,
        )
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        effects = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        stored = sm._session_runtime.record_receipt

        def fail_after_receipt(*args):
            stored(*args)
            raise RuntimeError("late save receipt failure")

        with monkeypatch.context() as patch:
            patch.setattr(sm._session_runtime, "record_receipt", fail_after_receipt)
            with pytest.raises(RuntimeError, match="late save receipt failure"):
                sm.submit_command(answer, package, context=context)
        assert sm.get_command_receipt(sid, answer.command_id, package, context=context) is None
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == effects
        assert sm.submit_command(answer, package, context=context).status == "committed"


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"uses": 0}, "feature_ineligible"),
        ({"hidden": True}, "feature_ineligible"),
        ({"npc_cell": "15,0"}, "feature_ineligible"),
    ],
)
def test_ineligible_grants_do_not_pay_or_draw(tmp_path, context, options, error):
    db, sid, package, loader, gm = make_bard_world(tmp_path, context, **options)
    with StateMachine(db, dnd_loader=loader) as sm:
        command = grant_command(sm, sid, package, gm)
        before = sm.read_noncombat_snapshot(sid, package, context=gm)
        rng = sm.read_rng_context(sid, package, context=gm)
        receipt = sm.submit_command(command, package, context=gm)
        assert (receipt.status, receipt.error_code) == ("rejected", error)
        assert sm.read_noncombat_snapshot(sid, package, context=gm) == before
        assert sm.read_rng_context(sid, package, context=gm) == rng


def test_grant_requires_control_and_selected_other_creature(bard_world):
    db, sid, package, loader, context = bard_world
    with StateMachine(db, dnd_loader=loader) as sm:
        command = grant_command(sm, sid, package, context)
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, package, context=replace(context, controlled_npc_ids=()))
        assert denied.value.code == "unauthorized"
        self_grant = replace(
            command,
            command_id="self-grant",
            payload=command.payload.model_copy(update={"target_id": NPC}),
        )
        receipt = sm.submit_command(self_grant, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "target_invalid")


def test_late_receipt_failure_rolls_back_paid_grant(bard_world, monkeypatch):
    db, sid, package, loader, context = bard_world
    with StateMachine(db, dnd_loader=loader) as sm:
        command = grant_command(sm, sid, package, context)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        record = sm._session_runtime.record_receipt

        def fail_after_receipt(*args):
            record(*args)
            raise RuntimeError("late grant receipt failure")

        with monkeypatch.context() as patch:
            patch.setattr(sm._session_runtime, "record_receipt", fail_after_receipt)
            with pytest.raises(RuntimeError, match="late grant receipt failure"):
                sm.submit_command(command, package, context=context)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.submit_command(command, package, context=context).status == "committed"


def test_json_feature_command_uses_async_public_boundary(bard_world):
    db, sid, package, loader, context = bard_world
    with StateMachine(db, dnd_loader=loader) as sm:
        typed = grant_command(sm, sid, package, context, "json-grant")
        command = world_command_from_data(
            {
                "schema_version": typed.schema_version,
                "session_id": typed.session_id,
                "command_id": typed.command_id,
                "actor_id": typed.actor_id,
                "command_type": typed.command_type,
                "payload": typed.payload.model_dump(mode="json"),
                "expected_world_version": typed.expected_world_version,
            }
        )
        assert command == typed
        receipt = asyncio.run(sm.submit_command_async(command, package, context=context))
        assert receipt.status == "committed"
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) == receipt


def test_grant_reopens_and_expires_at_exact_bound_clock(bard_world):
    db, sid, package, loader, context = bard_world
    context = replace(context, can_advance_time=True)
    with StateMachine(db, dnd_loader=loader) as sm:
        command = grant_command(sm, sid, package, context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        with sm._store.transaction():
            held = sm._world.characters.effects.held_bardic_inspiration(
                sid, command.payload.target_id, 0
            )
        assert (held.grant_id, held.die_sides, held.expires_tick) == ("grant", 8, 3600)
    with StateMachine(db, dnd_loader=loader) as sm:
        assert sm.submit_command(command, package, context=context) == receipt
        with sm._store.transaction():
            assert (
                sm._world.characters.effects.held_bardic_inspiration(
                    sid, command.payload.target_id, 0
                )
                == held
            )
        state = sm.read_session(sid, package, context=context)
        advance = TypedCommand(
            sid,
            "expire-grant",
            state.pc_actor_id,
            "session.advance_time",
            AdvanceSessionTime(3600),
            state.world_version,
        )
        assert sm.submit_command(advance, package, context=context).status == "committed"
        with sm._store.transaction():
            assert (
                sm._world.characters.effects.held_bardic_inspiration(
                    sid, command.payload.target_id, 3600
                )
                is None
            )
        assert not sm.read_noncombat_snapshot(sid, package, context=context).effect_states


def test_bardic_grant_reads_stay_fixed_as_remote_state_grows(tmp_path, context):
    counts = []
    for size in (0, 25):
        directory = tmp_path / str(size)
        directory.mkdir()
        db, sid, package, loader, authorized = make_bard_world(directory, context)
        with StateMachine(db, dnd_loader=loader) as sm:
            with sm._store.transaction(write=True):
                sm.scene_lifecycle.ensure_scene(sid, B, package)
            _grow_remote(sm, sid, size)
        with StateMachine(db, dnd_loader=loader) as sm:
            command = grant_command(sm, sid, package, authorized)
            with measure(sm) as (work, _):
                receipt = sm.submit_command(command, package, context=authorized)
            assert receipt.status == "committed", receipt
            counts.append((work["sql_selects"], work["sql_rows"]))
    assert counts[0] == counts[1]
