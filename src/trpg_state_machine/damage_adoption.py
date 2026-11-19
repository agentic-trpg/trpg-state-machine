"""Admit typed damage edits against a fixed encounter before any domain writes."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.damage_rules import damage_balances

from .errors import StateMachineError

DAMAGE_OPERATIONS = {
    delta.HPDelta,
    delta.TempHPSet,
    delta.DeathStateUpdate,
    delta.DamageAttributionUpdate,
    delta.DamageSequenceUpdate,
    delta.ProcessedDamageUpdate,
    delta.DeathLedgerUpdate,
}


def json_value(value):
    """Convert models and nested data into JSON-compatible values."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (tuple, list, set, frozenset)):
        return [json_value(v) for v in value]
    return value


def _invalid(message):
    """Reject damage outside the fixed command or its result evidence."""
    raise StateMachineError("invalid_rule_result", message)


def ordinary_damage_target(state, target):
    """Identify the living enemy NPC slice whose death has no party/effect lifecycle."""
    return (
        target.entity_type in {"NPC", "Monster"}
        and target.entity_id in state.encounter_ids
        and target.entity_id not in state.party_ids
        and target.entity_id not in state.dead_ids
        and target.entity_id not in state.concentration_chain
        and target.is_alive
        and target.hp_current > 0
    )


def validate_damage_result(request, result, target, *, source_id, is_crit):
    """Check one ordinary damage instance or a miss and return newly dead identities.

    RE owns damage, zero-HP consequences and their evidence. This check binds that
    evidence to the guarded edits without rerunning rolls or the death lifecycle.
    """
    snapshot = request.state_snapshot
    state = snapshot.combat_state
    actor_id = request.actor_id
    target_id = target.entity_id
    ops = {}
    for op in result.state_delta.operations:
        op_type = type(op)
        if op_type in DAMAGE_OPERATIONS:
            if op_type in ops:
                _invalid("Duplicate damage operation")
            ops[op_type] = op

    events = [event for event in result.proposed_events if event.type == "damage_applied"]
    deaths = [event for event in result.proposed_events if event.type == "death"]
    sequence = state.damage_instance_sequence + 1
    instance = f"damage:{sequence}:{actor_id}:{target_id}"
    if any(
        event.target_id != target_id
        or event.source_actor_id != actor_id
        or event.source_id != source_id
        or event.source_parent_id is not None
        or event.damage_instance_id != instance
        or event.amount < 0
        or event.is_crit != is_crit
        for event in events
    ):
        _invalid("Damage event differs from the authorized source, target or instance")

    hp, temp, _ = damage_balances(target.hp_current, target.temp_hp, sum(e.amount for e in events))
    attribution = actor_id if events and actor_id != target_id else target.last_damaged_by
    balances = (
        (delta.HPDelta, target.hp_current, hp, "resulting_hp"),
        (delta.TempHPSet, target.temp_hp, temp, "temp_hp"),
        (delta.DamageAttributionUpdate, target.last_damaged_by, attribution, "value"),
        (
            delta.DamageSequenceUpdate,
            state.damage_instance_sequence,
            sequence if events else state.damage_instance_sequence,
            "value",
        ),
    )
    for op_type, before, after, field in balances:
        op = ops.get(op_type)
        if (op is not None) != (before != after) or (
            op is not None and getattr(op, field) != after
        ):
            _invalid("Damage edits differ from the resolved damage balances or metadata")

    lethal = hp == 0
    life = ops.get(delta.DeathStateUpdate)
    ledger = ops.get(delta.DeathLedgerUpdate)
    if delta.ProcessedDamageUpdate in ops:
        _invalid("Ordinary damage cannot change the processed zero-HP ledger")
    if not lethal:
        if life is not None or ledger is not None or deaths:
            _invalid("Damage without a death cannot change death state or history")
        return set()

    if (
        not ordinary_damage_target(state, target)
        or life is None
        or life.value.is_alive
        or life.value.death_saves != target.death_saves
        or ledger is None
        or ledger.dead_ids - ledger.expected_dead_ids != {target_id}
        or len(ledger.records) != len(ledger.expected_records) + 1
        or len(deaths) != 1
        or deaths[0].target_id != target_id
        or deaths[0].reason != "damage"
        or ledger.records[-1].reason != deaths[0].reason
    ):
        _invalid("Zero-HP ordinary damage requires matching death evidence, state and ledger")
    return {target_id}


def adopt_damage_operation(request, target_kind, op, edit):
    """Match one typed damage edit to its owner and pass guarded fields to the candidate."""
    snapshot = request.state_snapshot
    target = request.payload.target_id
    source = request.actor_id
    combat = snapshot.combat_state.combat_id
    if getattr(op, "target_id", target) != target or getattr(op, "combat_id", combat) != combat:
        _invalid("Damage operation belongs to another target or combat")
    if type(op) is delta.HPDelta:
        if op.amount > 0:
            _invalid("Damage cannot restore HP")
        edit("actor", target, "hp_current", op.expected_hp, op.resulting_hp)
    elif type(op) is delta.TempHPSet:
        if op.temp_hp > op.expected_temp_hp:
            _invalid("Damage cannot grant temporary HP")
        edit("actor", target, "temp_hp", op.expected_temp_hp, op.temp_hp)
    elif type(op) is delta.DeathStateUpdate:
        for field in type(op.value).model_fields:
            edit("actor", target, field, getattr(op.expected, field), getattr(op.value, field))
    elif type(op) is delta.DamageAttributionUpdate:
        if op.value not in (None, source):
            _invalid("Damage attribution belongs to another actor")
        edit("actor", target, "last_damaged_by", op.expected, op.value)
    elif type(op) is delta.DamageSequenceUpdate:
        if op.value < op.expected:
            _invalid("Damage sequence cannot move backwards")
        edit("combat", combat, "damage_instance_sequence", op.expected, op.value)
    elif type(op) is delta.ProcessedDamageUpdate:
        if not op.expected <= op.value or any(
            actor != target for actor, _ in op.value - op.expected
        ):
            _invalid(
                "Processed damage history must be preserved and additions must match the target"
            )
        edit("combat", combat, "processed_zero_hp_damage_instances", op.expected, op.value)
    elif type(op) is delta.DeathLedgerUpdate:
        if (
            not op.expected_dead_ids <= op.dead_ids
            or op.records[: len(op.expected_records)] != op.expected_records
        ):
            _invalid("Death history cannot be erased or rewritten")
        new_dead_ids = op.dead_ids - op.expected_dead_ids
        records = op.records[len(op.expected_records) :]
        if new_dead_ids - {target} or [record.target_id for record in records] != sorted(
            new_dead_ids
        ):
            _invalid("Death identities and records must match the declared target exactly once")
        if any(
            record.killer_id != source
            or record.location_id != snapshot.scene_state.scene_id
            or record.target_kind != target_kind
            for record in records
        ):
            _invalid("Death record attribution differs from the authorized damage")
        edit("combat", combat, "dead_ids", op.expected_dead_ids, op.dead_ids)
        edit("combat", combat, "deaths_recorded", op.expected_records, op.records)
