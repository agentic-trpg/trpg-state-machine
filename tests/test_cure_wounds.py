"""Public Cure Wounds acceptance with an authored class source and real RE assets."""

from copy import deepcopy
from dataclasses import asdict, replace
from random import Random

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_delta import (
    ActionBudgetUpdate,
    AttackBudgetState,
    MovementLedgerUpdate,
)
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import create, initialize
from item_support import NPC, A, item_data, item_loader, submit
from test_combat_healing_potion import FIRST

from trpg_state_machine import (
    PickupItem,
    StateMachine,
    StateMachineError,
    TimeBinding,
    TypedCommand,
)
from trpg_state_machine.package import _validate

SLOT = "spell:1"
SOURCE = "class:cleric"
SPELL = "cure-wounds"
HEALING_WORD = "healing-word"
BYSTANDER = "npc.attendant"


def cure_loader(*, equipped=False, extra_spells=(), with_potion=False):
    """Bind the canonical first-level cleric and Cure Wounds to authored items."""
    base = item_loader()
    canonical = BundledAssetLoader()
    return MemoryAssetLoader(
        classes=[canonical.get_class("cleric")],
        features=[
            canonical.get_feature(s)
            for s in (
                "spellcasting",
                "divine-order",
                *(("divine-order-protector",) if equipped else ()),
            )
        ],
        spells=[canonical.get_spell(spell) for spell in (SPELL, *extra_spells)],
        items=list(base._items.values())
        + ([canonical.get_item(s) for s in ("mace", "chain-shirt")] if equipped else [])
        + ([canonical.get_item("potion-of-healing")] if with_potion else []),
        monsters=list(base._monsters.values()),
    )


def cure_world(
    tmp_path,
    context,
    *,
    hp=2,
    slot_count=2,
    prepared=True,
    verbal=True,
    solo_dead=False,
    next_actor_spent=False,
    caster_spent_movement=False,
    equipped=False,
    extra_spells=(),
    rng_seed=0,
    target_hp=None,
    target_temp_hp=0,
    second_enemy=False,
    same_attribution=False,
    with_potion=False,
    timed=False,
    exploration=False,
    target_cell="1,0",
    loader=None,
    configure=None,
):
    """Create a bound exploration session or initialize combat for the same cast."""
    loader = loader or cure_loader(
        equipped=equipped, extra_spells=extra_spells, with_potion=with_potion
    )
    data = item_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    data["manifest"]["package_version"] = "spell-01/1"
    data["pc_templates"][0]["character"]["hp"]["current"] = hp
    data["pc_templates"][0]["character"]["inventory"] = []
    if with_potion:
        data["scenes"][0]["objects"].append(
            dict(
                object_id=FIRST, state=None, movable=False, blocks_passage=False, initial_cell="0,0"
            )
        )
        data["item_transfers"].append(
            dict(
                instance_id=FIRST,
                item_slug="potion-of-healing",
                initial_owner_id=None,
                quantity=1,
                charges_remaining_per_unit=1,
                policy="ordinary_public",
            )
        )
    if equipped:
        owner = data["pc_templates"][0]["pc_template_id"]
        data["pc_templates"][0]["character"]["inventory"] = ["mace", "chain-shirt"]
        for oid, slug in (("object.caster_mace", "mace"), ("object.caster_armor", "chain-shirt")):
            data["scenes"][0]["objects"].append(
                {
                    "object_id": oid,
                    "state": None,
                    "movable": False,
                    "blocks_passage": False,
                    "initial_cell": "2,1",
                }
            )
            data["item_transfers"].append(
                {
                    "instance_id": oid,
                    "item_slug": slug,
                    "initial_owner_id": owner,
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "policy": "ordinary_public",
                }
            )
    target = next(actor for actor in data["actors"] if actor["actor_id"] == NPC)
    target["initial_cell"] = target_cell
    if second_enemy:
        second = deepcopy(target)
        second.update(actor_id=BYSTANDER, name="Attendant", initial_cell="2,0")
        second["npc_state"].update(
            initial_knowledge_ids=[], memories=[], beliefs=[], relationships=[], goals=[], plans=[]
        )
        data["actors"].append(second)
        data["scenes"][0]["actor_ids"].append(BYSTANDER)
        data["navigation"]["actor_grants"].append(
            {"actor_id": BYSTANDER, "region_ids": ["region.public"]}
        )
    for foundation in data["rule_bindings"].values():
        foundation["rng_state"] = RNGState.capture(Random(rng_seed)).model_dump(mode="json")
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/20"
        pc = snapshot["character_states"][0]
        pc.update(
            hp_current=hp,
            class_slug="cleric",
            classes={"cleric": 1},
            granted_features=["spellcasting", "divine-order"],
            character_level=1,
            wisdom=16,
            spellcasting_ability="wis",
            spell_slots={1: slot_count},
            carried_item_slugs=[],
            weapon_in_hands=None,
            weapon_grip="none",
        )
        pc.pop("spells_known", None)
        if caster_spent_movement:
            pc["movement_remaining"] = pc["base_speed"] - 10
            if "combat_state" in snapshot:
                ledger = snapshot["combat_state"]["movement_ledgers"][pc["entity_id"]]
                ledger.update(spent_ft=10, distance_ft=10)
        npc = snapshot["character_states"][1]
        npc.update(
            hp_current=0 if solo_dead else (hp if target_hp is None else target_hp),
            hp_max=50,
            temp_hp=target_temp_hp,
            is_alive=not solo_dead,
            **({"granted_features": []} if extra_spells else {}),
        )
        if same_attribution:
            npc["last_damaged_by"] = pc["entity_id"]
        if next_actor_spent:
            npc.update(action_available=False, action_taken_this_turn=True, movement_remaining=0)
        npc.pop("spells_known", None)
        if second_enemy:
            snapshot["character_states"].append(
                {
                    **deepcopy(npc),
                    "entity_id": BYSTANDER,
                    "name": "Attendant",
                    "initiative": 0,
                    "hp_current": 40,
                    "temp_hp": 0,
                    "is_alive": True,
                }
            )
            if "combat_state" in snapshot:
                combat = snapshot["combat_state"]
                for key in ("initiative_ids", "encounter_ids"):
                    combat[key].append(BYSTANDER)
                combat["actor_zone"][BYSTANDER] = "2,0"
                for key in ("movement_ledgers", "monster_slug_by_entity", "xp_value_by_entity"):
                    combat[key][BYSTANDER] = deepcopy(combat[key][NPC])
            else:
                foundation["xp"][BYSTANDER] = 0
        if solo_dead and "combat_state" in snapshot:
            snapshot["combat_state"]["dead_ids"] = [npc["entity_id"]]
            snapshot["combat_state"]["deaths_recorded"] = [
                {
                    "target_id": npc["entity_id"],
                    "target_kind": npc["entity_type"].lower(),
                    "location_id": snapshot["scene_state"]["scene_id"],
                    "reason": "damage",
                    "killer_id": pc["entity_id"],
                }
            ]
        snapshot["inventory_state"]["entries"] = [
            entry
            for entry in snapshot["inventory_state"]["entries"]
            if entry["owner_id"] != pc["entity_id"]
        ]
        if equipped:
            pc.update(
                strength=16,
                dexterity=14,
                ac=15,
                save_proficiencies=["wis", "cha"],
                weapon_proficiencies=[
                    "simple_melee",
                    "simple_ranged",
                    "martial_melee",
                    "martial_ranged",
                ],
                granted_features=["spellcasting", "divine-order", "divine-order-protector"],
                carried_item_slugs=["mace", "chain-shirt"],
                weapon_in_hands="mace",
                weapon_grip="one_handed",
                held_weapon_instance_id="object.caster_mace",
                worn_armor="medium",
                worn_armor_instance_id="object.caster_armor",
            )
            snapshot["inventory_state"]["entries"].extend(
                {
                    "instance_id": oid,
                    "owner_id": owner,
                    "item_slug": slug,
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
                for oid, slug in (
                    ("object.caster_mace", "mace"),
                    ("object.caster_armor", "chain-shirt"),
                )
            )
        snapshot["resource_state"] = {
            "pools": [
                {
                    "kind": "spell_slot",
                    "owner_id": pc["entity_id"],
                    "pool_id": SLOT,
                    "current": slot_count,
                    "maximum": 2,
                    "level": 1,
                }
            ]
        }
        snapshot["casting_state"] = {
            "actors": [
                {
                    "actor_id": pc["entity_id"],
                    "verbal_available": verbal,
                    "sources": [
                        {
                            "source_id": SOURCE,
                            "class_slug": "cleric",
                            "ability": "wis",
                            "spells": [
                                {
                                    "spell_id": spell,
                                    "prepared_order": index
                                    if prepared and loader.get_spell(spell).level
                                    else None,
                                    "material": None,
                                }
                                for index, spell in enumerate((SPELL, *extra_spells))
                            ],
                        }
                    ],
                }
            ]
        }
    if timed or exploration:
        data["time_binding"] = asdict(TimeBinding())
        for person in data["actors"]:
            person.pop("npc_state", None)
    if configure is not None:
        configured_loader = configure(data, loader)
        if configured_loader is not None:
            loader = configured_loader
            data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    package = _validate(data)
    db = tmp_path / "cure-wounds.sqlite"
    sm = StateMachine(db, dnd_loader=loader)
    sid = create(sm, package, context)
    if with_potion:
        picked = submit(sm, sid, package, context, PickupItem(FIRST, A, "0,0"), "pick.potion")
        assert picked.status == "committed", picked
    if not exploration:
        assert initialize(sm, sid, package, context).status == "committed"
    return sm, sid, package, loader, db


def cast_command(
    sm, sid, package, context, *, key="cast", target=None, source_actor=False, spell=SPELL
):
    """Build a typed public spell intent at the current world version."""
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        state.pc_actor_id,
        "combat.intent",
        CombatIntentPayload(
            intent_type="cast_spell",
            source_id=state.pc_actor_id if source_actor else None,
            spell_id=spell,
            target_id=target or state.pc_actor_id,
            slot_level=1,
            spell_source_id=SOURCE,
        ),
        state.world_version,
    )


def pool(snapshot):
    """Return the one authored level-one slot balance."""
    return next(p for p in snapshot.resource_state.pools if p.pool_id == SLOT)


def actor(snapshot, actor_id):
    """Select the exact actor from the mechanical snapshot."""
    return next(a for a in snapshot.character_states if a.entity_id == actor_id)


def test_healing_word_retains_turn_and_blocks_second_paid_spell(tmp_path, context):
    """The public cast pays one bonus action and one owned level-one slot."""
    sm, sid, package, loader, db = cure_world(tmp_path, context, extra_spells=(HEALING_WORD,))
    with sm:
        command = cast_command(sm, sid, package, context, spell=HEALING_WORD, target=NPC)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        caster = actor(after, command.actor_id)
        assert caster.action_available and not caster.action_taken_this_turn
        assert not caster.bonus_action_available and caster.bonus_action_taken_this_turn
        assert caster.spell_slots == {1: 1}
        assert pool(after).current == 1
        assert after.combat_state.expended_resources[caster.entity_id] == {"spell_slot:1": 1}
        assert after.combat_state.turn_serial == before.combat_state.turn_serial
        assert after.combat_state.current_turn_index == before.combat_state.current_turn_index
        assert after.combat_state.round_number == before.combat_state.round_number
        assert after.combat_state.movement_ledgers == before.combat_state.movement_ledgers
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current + 11
        next_rng = sm.read_rng_context(sid, package, context=context)
        assert next_rng != rng
        assert sm.submit_command(command, package, context=context) == receipt
        for spell, code in ((HEALING_WORD, "no_action_economy"), (SPELL, "spell.turn_history")):
            repeat = cast_command(sm, sid, package, context, key=f"repeat.{spell}", spell=spell)
            refused = sm.submit_command(repeat, package, context=context)
            assert refused.error_code == code
            assert not refused.event_seqs
            assert sm.read_combat_snapshot(sid, package, context=context) == after
            assert sm.read_rng_context(sid, package, context=context) == next_rng
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == next_rng


@pytest.mark.parametrize("with_potion", [False, True], ids=["empty", "picked-up-potion"])
def test_healing_word_budget_resets_on_real_turn_wrap(tmp_path, context, with_potion):
    """Explicit end turn refreshes both budgets and permits a new paid cast."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, extra_spells=(HEALING_WORD,), with_potion=with_potion, timed=True
    )
    with sm:
        command = cast_command(sm, sid, package, context, spell=HEALING_WORD)
        initial = sm.read_combat_snapshot(sid, package, context=context)
        first_tick = sm.read_session(sid, package, context=context).world.game_tick
        if with_potion:
            stock = [
                entry for entry in initial.inventory_state.entries if entry.instance_id == FIRST
            ]
            assert len(stock) == 1
            assert stock[0].owner_id == command.actor_id
        assert sm.submit_command(command, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        cast_rng = sm.read_rng_context(sid, package, context=context)
        assert before.combat_state.turn_serial == initial.combat_state.turn_serial
        assert sm.read_session(sid, package, context=context).world.game_tick == first_tick
        for actor_id in (command.actor_id, NPC):
            state = sm.read_combat_snapshot(sid, package, context=context)
            end_turn = replace(
                command,
                command_id=f"pass.{actor_id}",
                actor_id=actor_id,
                payload=CombatIntentPayload(intent_type="pass"),
                expected_world_version=state.world_version,
            )
            receipt = sm.submit_command(end_turn, package, context=context)
            assert receipt.status == "committed", receipt
            assert sm.read_session(sid, package, context=context).world.game_tick == (
                first_tick if actor_id == command.actor_id else first_tick + 6
            )
        reset = sm.read_combat_snapshot(sid, package, context=context)
        caster = actor(reset, command.actor_id)
        assert reset.combat_state.turn_serial == before.combat_state.turn_serial + 2
        assert reset.combat_state.round_number == before.combat_state.round_number + 1
        assert caster.action_available and not caster.action_taken_this_turn
        assert caster.bonus_action_available and not caster.bonus_action_taken_this_turn
        assert pool(reset).current == 1
        assert reset.combat_state.expended_resources == before.combat_state.expended_resources
        assert reset.inventory_state == before.inventory_state == initial.inventory_state
        assert sm.read_rng_context(sid, package, context=context) == cast_rng
        next_cast = cast_command(sm, sid, package, context, key="next.cast", spell=HEALING_WORD)
        assert sm.submit_command(next_cast, package, context=context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert pool(after).current == 0
        assert after.combat_state.expended_resources[command.actor_id] == {"spell_slot:1": 2}


@pytest.mark.parametrize("distinct_target", [False, True])
def test_cure_wounds_commits_healing_payment_and_reopens(tmp_path, context, distinct_target):
    sm, sid, package, loader, db = cure_world(tmp_path, context)
    with sm:
        target = NPC if distinct_target else None
        command = cast_command(
            sm, sid, package, context, target=target, source_actor=distinct_target
        )
        target_id = target or command.actor_id
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        assert receipt.event_seqs
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert actor(after, target_id).hp_current > actor(before, target_id).hp_current
        assert actor(after, command.actor_id).spell_slots == {1: 1}
        assert pool(after).current == pool(before).current - 1 == 1
        assert after.combat_state.expended_resources[command.actor_id]["spell_slot:1"] == 1
        assert not actor(after, command.actor_id).action_available
        assert sm.read_rng_context(sid, package, context=context) != rng
        assert after.world_version > before.world_version
        assert before.resource_state.pools[0].current == 2
        assert actor(before, target_id).hp_current == 2
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) == receipt
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_records WHERE session_id=? AND source_command_id=?",
                (sid, command.command_id),
            ).fetchone()[0]
            > 0
        )
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchone() is None
    with StateMachine(db, dnd_loader=loader) as reopened:
        restored = reopened.read_combat_snapshot(sid, package, context=context)
        assert restored == after
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )
        assert (
            reopened._conn.execute(
                "SELECT current FROM character_resources WHERE session_id=? AND actor_id=? "
                "AND resource_id=?",
                (sid, command.actor_id, SLOT),
            ).fetchone()[0]
            == 1
        )
        assert (
            reopened._conn.execute(
                "SELECT current_hp FROM characters WHERE session_id=? AND actor_id=?",
                (sid, target_id),
            ).fetchone()[0]
            == actor(after, target_id).hp_current
        )


def test_equipped_cleric_keeps_item_and_spell_refs_after_cure(tmp_path, context):
    """A real spell commit preserves equipped identity and retained owner fields."""
    sm, sid, package, loader, db = cure_world(tmp_path, context, equipped=True)
    with sm:
        aid = sm.read_session(sid, package, context=context).pc_actor_id
        before = sm.read_combat_snapshot(sid, package, context=context)
        old_equipment = sm._characters.equipment(sid, aid)
        old_spells = sm._characters.all_spells(sid, aid)
        old_prepared = sm._characters.prepared_spells(sid, aid)
        assert actor(before, aid).held_weapon_instance_id == "object.caster_mace"
        assert old_equipment.weapon.object_id == "object.caster_mace"
        assert old_prepared[0] is old_spells[0]
        assert (
            sm.submit_command(
                cast_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert actor(after, aid).held_weapon_instance_id == "object.caster_mace"
        assert actor(after, aid).worn_armor_instance_id == "object.caster_armor"
        assert actor(after, aid).hp_current > actor(before, aid).hp_current
        assert pool(after).current == pool(before).current - 1
        assert sm._characters.all_spells(sid, aid)[0] is old_spells[0]
        assert sm._characters.prepared_spells(sid, aid)[0] is old_prepared[0]
        assert sm._characters.equipment(sid, aid).weapon.object_id == "object.caster_mace"
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after


def test_full_hp_still_pays_and_draws(tmp_path, context):
    sm, sid, package, _, _ = cure_world(tmp_path, context, hp=8)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        assert sm.submit_command(command, package, context=context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert actor(after, command.actor_id).hp_current == actor(before, command.actor_id).hp_max
        assert pool(after).current == 1
        assert sm.read_rng_context(sid, package, context=context) != rng


def test_only_living_caster_wraps_round_and_pays_slot(tmp_path, context):
    """A paid cast may reset its own action when no other actor can take a turn."""
    sm, sid, package, _, _ = cure_world(tmp_path, context, solo_dead=True)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert before.combat_state.dead_ids == {NPC}
        assert (
            before.combat_state.initiative_ids[before.combat_state.current_turn_index]
            == command.actor_id
        )
        before_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state.current_turn_index == before.combat_state.current_turn_index
        assert after.combat_state.round_number == before.combat_state.round_number + 1
        assert actor(after, command.actor_id).action_available
        assert (
            actor(after, command.actor_id).hp_current > actor(before, command.actor_id).hp_current
        )
        assert pool(after).current == pool(before).current - 1
        assert after.combat_state.expended_resources[command.actor_id]["spell_slot:1"] == 1
        assert sm.read_rng_context(sid, package, context=context) != before_rng
        assert sm.submit_command(command, package, context=context) == receipt


def test_spent_next_actor_receives_ordinary_turn_reset(tmp_path, context):
    """A genuine handoff restores a previously spent actor's bounded turn budget."""
    sm, sid, package, _, _ = cure_world(tmp_path, context, next_actor_spent=True)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert not actor(before, NPC).action_available
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert actor(after, NPC).action_available
        assert not actor(after, NPC).action_taken_this_turn
        assert actor(after, NPC).movement_remaining == actor(before, NPC).base_speed
        assert after.combat_state.movement_ledgers[NPC].spent_ft == 0
        assert after.combat_state.movement_ledgers[NPC].distance_ft == 0


def test_caster_spent_movement_retains_ledger_derived_balance(tmp_path, context):
    """A lawful cast preserves the outgoing actor's previously spent movement."""
    sm, sid, package, _, _ = cure_world(tmp_path, context, caster_spent_movement=True)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert before.combat_state.movement_ledgers[command.actor_id].spent_ft == 10
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert actor(after, command.actor_id).movement_remaining == (
            actor(before, command.actor_id).base_speed - 10
        )
        assert after.combat_state.movement_ledgers[command.actor_id].spent_ft == 10


@pytest.mark.parametrize(
    ("change", "code"),
    [("unprepared", "spell.source"), ("no_verbal", "spell.source"), ("no_slot", "no_slot")],
)
def test_ineligible_cast_preserves_all_facts(tmp_path, context, change, code):
    sm, sid, package, _, _ = cure_world(
        tmp_path,
        context,
        prepared=change != "unprepared",
        verbal=change != "no_verbal",
        slot_count=0 if change == "no_slot" else 2,
    )
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm._conn.execute(
            "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
        ).fetchone()[0]
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", code)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) == receipt
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == before_events
        )


@pytest.mark.parametrize("spell", [SPELL, HEALING_WORD])
def test_healing_spell_occ_and_late_failure_rollback(tmp_path, context, spell):
    extra_spells = (HEALING_WORD,) if spell == HEALING_WORD else ()
    sm, sid, package, _, _ = cure_world(tmp_path, context, extra_spells=extra_spells)
    with sm:
        command = cast_command(sm, sid, package, context, spell=spell)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm._conn.execute(
            "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
        ).fetchone()[0]
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_cure_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='cast' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(command, package, context=context)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == before_events
        )
        sm._conn.execute("DROP TRIGGER fail_cure_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(command, package, context=context) == receipt
        committed = sm.read_combat_snapshot(sid, package, context=context)
        committed_rng = sm.read_rng_context(sid, package, context=context)
        stale = replace(command, command_id="cast.stale")
        conflict = sm.submit_command(stale, package, context=context)
        assert (conflict.status, conflict.error_code) == ("conflict", "version_conflict")
        assert sm.get_command_receipt(sid, stale.command_id, package, context=context) == conflict
        assert sm.read_combat_snapshot(sid, package, context=context) == committed
        assert sm.read_rng_context(sid, package, context=context) == committed_rng


@pytest.mark.parametrize("field", ["departed_actor_id", "monster_turn_start_done"])
def test_forged_turn_reference_rolls_back_all_spell_facts(tmp_path, context, field):
    """An accepted RE envelope cannot add unrelated combat turn references."""
    sm, sid, package, _, _ = cure_world(tmp_path, context)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_version = sm.read_session(sid, package, context=context).world_version
        before_events = sm._conn.execute(
            "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
        ).fetchone()[0]
        assert getattr(before.combat_state, field) is None
        original = sm._rules._evaluate_dnd

        async def forged(request):
            """Change one unrelated turn field after the real evaluator accepts."""
            result = await original(request)
            assert result.status == "accepted"
            operations = list(result.state_delta.operations)
            index = next(i for i, op in enumerate(operations) if op.kind == "combat.turn_update")
            turn = operations[index]
            forged_value = (
                command.actor_id
                if field == "departed_actor_id"
                else (before.combat_state.round_number, command.actor_id)
            )
            operations[index] = turn.model_copy(
                update={"value": turn.value.model_copy(update={field: forged_value})}
            )
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    )
                }
            )

        sm._rules._evaluate_dnd = forged
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after == before
        assert (
            actor(after, command.actor_id).hp_current == actor(before, command.actor_id).hp_current
        )
        assert pool(after) == pool(before)
        assert after.combat_state.expended_resources == before.combat_state.expended_resources
        assert (
            actor(after, command.actor_id).action_available
            == actor(before, command.actor_id).action_available
        )
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_session(sid, package, context=context).world_version == before_version
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == before_events
        )
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [("extra_actions_remaining", 1), ("attacks_remaining", 2)],
)
def test_forged_next_actor_budget_rolls_back_spell(tmp_path, context, field, forged_value):
    """A paid cast cannot grant the following actor extra actions or attacks."""
    sm, sid, package, _, _ = cure_world(tmp_path, context)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm._conn.execute(
            "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
        ).fetchone()[0]
        original = sm._rules._evaluate_dnd

        async def forged(request):
            """Add a false next-turn budget credit to an otherwise accepted result."""
            result = await original(request)
            assert result.status == "accepted"
            operations = list(result.state_delta.operations)
            turn = next(op for op in operations if op.kind == "combat.turn_update")
            next_actor = before.combat_state.initiative_ids[turn.value.current_turn_index]
            budget = next(
                (
                    op
                    for op in operations
                    if op.kind == "combat.action_budget_update" and op.actor_id == next_actor
                ),
                None,
            )
            if budget is None:
                next_before = actor(before, next_actor)
                old = AttackBudgetState.model_validate(
                    {name: getattr(next_before, name) for name in AttackBudgetState.model_fields}
                )
                operations.append(
                    ActionBudgetUpdate(
                        kind="combat.action_budget_update",
                        actor_id=next_actor,
                        expected=old,
                        value=old.model_copy(update={field: forged_value}),
                    )
                )
            else:
                index = operations.index(budget)
                operations[index] = budget.model_copy(
                    update={"value": budget.value.model_copy(update={field: forged_value})}
                )
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    )
                }
            )

        sm._rules._evaluate_dnd = forged
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == before_events
        )
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


def test_missing_turn_start_event_rolls_back_spell(tmp_path, context):
    """An accepted Delta must retain the event proving its next actor handoff."""
    sm, sid, package, _, _ = cure_world(tmp_path, context)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm._conn.execute(
            "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
        ).fetchone()[0]
        original = sm._rules._evaluate_dnd

        async def without_turn_start(request):
            """Remove one genuine turn event after RE accepts the cast."""
            result = await original(request)
            assert result.status == "accepted"
            events = tuple(
                event for event in result.proposed_events if event.type != "turn_started"
            )
            assert len(events) == len(result.proposed_events) - 1
            return result.model_copy(update={"proposed_events": events})

        sm._rules._evaluate_dnd = without_turn_start
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == before_events
        )
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


@pytest.mark.parametrize("fault", ["forged_move", "omitted_budget"])
def test_next_turn_reset_cannot_be_forged_or_omitted(tmp_path, context, fault):
    """A paid cast adopts only the ordinary next actor movement and action reset."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, next_actor_spent=fault == "omitted_budget"
    )
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm._conn.execute(
            "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
        ).fetchone()[0]
        original = sm._rules._evaluate_dnd

        async def tampered(request):
            """Insert a false movement spend or remove the real budget reset."""
            result = await original(request)
            assert result.status == "accepted"
            operations = list(result.state_delta.operations)
            turn = next(op for op in operations if op.kind == "combat.turn_update")
            next_actor = before.combat_state.initiative_ids[turn.value.current_turn_index]
            if fault == "forged_move":
                old = before.combat_state.movement_ledgers[next_actor]
                assert not any(
                    op.kind == "combat.movement_ledger_update" and op.actor_id == next_actor
                    for op in operations
                )
                operations.append(
                    MovementLedgerUpdate(
                        kind="combat.movement_ledger_update",
                        actor_id=next_actor,
                        expected=old,
                        value=old.model_copy(update={"spent_ft": 10, "distance_ft": 10}),
                    )
                )
            else:
                budget = [
                    op
                    for op in operations
                    if op.kind == "combat.action_budget_update" and op.actor_id == next_actor
                ]
                assert len(budget) == 1
                operations.remove(budget[0])
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    )
                }
            )

        sm._rules._evaluate_dnd = tampered
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == before_events
        )
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("movement_remaining", 999),
        ("sneak_attack_spent_this_turn", True),
        ("omit", None),
    ],
)
def test_outgoing_caster_budget_cannot_be_forged_or_omitted(tmp_path, context, field, forged_value):
    """An accepted cast must include its exact paid outgoing action budget."""
    sm, sid, package, _, _ = cure_world(tmp_path, context)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm._conn.execute(
            "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
        ).fetchone()[0]
        original = sm._rules._evaluate_dnd

        async def forged(request):
            """Change one returned caster budget field after RE accepts."""
            result = await original(request)
            assert result.status == "accepted"
            operations = list(result.state_delta.operations)
            index = next(
                i
                for i, op in enumerate(operations)
                if op.kind == "combat.action_budget_update" and op.actor_id == command.actor_id
            )
            budget = operations[index]
            if field == "omit":
                operations.pop(index)
            else:
                operations[index] = budget.model_copy(
                    update={"value": budget.value.model_copy(update={field: forged_value})}
                )
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    )
                }
            )

        sm._rules._evaluate_dnd = forged
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == before_events
        )
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
