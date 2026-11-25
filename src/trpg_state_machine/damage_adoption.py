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


def damage_result_balances(request, result, target, *, source_id, is_crit, actor_id=None):
    """Bind one damage packet or miss to its typed HP, temporary HP and attribution edits."""
    snapshot = request.state_snapshot
    state = snapshot.combat_state
    actor_id = request.actor_id if actor_id is None else actor_id
    target_id = target.entity_id
    ops = {}
    for op in result.state_delta.operations:
        op_type = type(op)
        if op_type in DAMAGE_OPERATIONS:
            if op_type in ops:
                _invalid("Duplicate damage operation")
            ops[op_type] = op

    events = [event for event in result.proposed_events if event.type == "damage_applied"]
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

    return hp, ops, instance


def validate_damage_result(request, result, target, *, source_id, is_crit, actor_id=None):
    """Check one ordinary damage instance or a miss and return newly dead identities.

    RE owns damage, zero-HP consequences and their evidence. This check binds that
    evidence to the guarded edits without rerunning rolls or the death lifecycle.
    """
    target_id = target.entity_id
    deaths = [event for event in result.proposed_events if event.type == "death"]
    hp, ops, _ = damage_result_balances(
        request, result, target, source_id=source_id, is_crit=is_crit, actor_id=actor_id
    )

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
        not ordinary_damage_target(request.state_snapshot.combat_state, target)
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


def adopt_death_state(op, edit):
    """Stage the typed life and save counters for the operation's verified actor."""
    for field in type(op.value).model_fields:
        edit("actor", op.target_id, field, getattr(op.expected, field), getattr(op.value, field))


def adopt_death_ledger(snapshot, op, target, killer, target_kind, edit):
    """Preserve committed history and stage only the verified terminal actor's record."""
    if (
        not op.expected_dead_ids <= op.dead_ids
        or op.records[: len(op.expected_records)] != op.expected_records
    ):
        _invalid("Death history cannot be erased or rewritten")
    new_dead_ids = op.dead_ids - op.expected_dead_ids
    records = op.records[len(op.expected_records) :]
    if new_dead_ids - {target} or [record.target_id for record in records] != sorted(new_dead_ids):
        _invalid("Death ledger additions must match the verified actor")
    if any(
        record.killer_id != killer
        or record.location_id != snapshot.scene_state.scene_id
        or record.target_kind != target_kind
        for record in records
    ):
        _invalid("Death record attribution differs from the authorized operation")
    edit("combat", op.combat_id, "dead_ids", op.expected_dead_ids, op.dead_ids)
    edit("combat", op.combat_id, "deaths_recorded", op.expected_records, op.records)


def adopt_damage_operation(request, target_kind, op, edit, *, target_id=None, source_id=None):
    """Match one typed damage edit to its owner and pass guarded fields to the candidate."""
    snapshot = request.state_snapshot
    target = target_id if target_id is not None else request.payload.target_id
    source = request.actor_id if source_id is None else source_id
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
        adopt_death_state(op, edit)
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
        killer = source if source != target else None
        adopt_death_ledger(snapshot, op, target, killer, target_kind, edit)


def validate_area_damage(request, result, targets, packets, *, source_id):
    """Bind ordered area damage packets to each actor's guarded edits and encounter history."""
    caster = request.actor_id
    snapshot = request.state_snapshot
    state = snapshot.combat_state
    actor_ops = (
        delta.HPDelta,
        delta.TempHPSet,
        delta.DamageAttributionUpdate,
        delta.DeathStateUpdate,
    )
    changes = {}
    sequence_op = None
    death_ledger = None
    for op in result.state_delta.operations:
        kind = type(op)
        if kind in actor_ops:
            key = kind, op.target_id
            if op.target_id not in targets or key in changes:
                _invalid("Area damage edit repeats or names an unaffected creature")
            changes[key] = op
        elif kind is delta.DamageSequenceUpdate:
            if sequence_op is not None:
                _invalid("Area damage repeats the encounter sequence edit")
            sequence_op = op
        elif kind is delta.DeathLedgerUpdate:
            if death_ledger is not None:
                _invalid("Area damage repeats the encounter death ledger edit")
            death_ledger = op
        elif kind is delta.ProcessedDamageUpdate:
            _invalid("Area damage cannot change the processed zero-HP ledger")

    initial_sequence = state.damage_instance_sequence
    dead = []
    for sequence, ((target_id, target), packet) in enumerate(
        zip(targets.items(), packets, strict=True), start=initial_sequence + 1
    ):
        if (
            not ordinary_damage_target(state, target)
            or packet.target_id != target_id
            or packet.source_actor_id != caster
            or packet.source_id != source_id
            or packet.source_parent_id is not None
            or packet.damage_instance_id != f"damage:{sequence}:{caster}:{target_id}"
            or packet.amount < 0
            or packet.is_crit
        ):
            _invalid("Area damage packet differs from its source, creature or sequence")
        hp, temp, _ = damage_balances(target.hp_current, target.temp_hp, packet.amount)
        for kind, before, after, field in (
            (delta.HPDelta, target.hp_current, hp, "resulting_hp"),
            (delta.TempHPSet, target.temp_hp, temp, "temp_hp"),
            (delta.DamageAttributionUpdate, target.last_damaged_by, caster, "value"),
        ):
            edit = changes.get((kind, target_id))
            if (edit is not None) != (before != after) or (
                edit is not None and getattr(edit, field) != after
            ):
                _invalid("Area damage edits differ from the resolved damage balances")
        life = changes.get((delta.DeathStateUpdate, target_id))
        if (life is not None) != (hp == 0) or (
            life is not None
            and (life.value.is_alive or life.value.death_saves != target.death_saves)
        ):
            _invalid("Area damage edits differ from the creature's resolved life state")
        if hp == 0:
            dead.append(target_id)

    deaths = [event for event in result.proposed_events if event.type == "death"]
    if (sequence_op is not None) != bool(targets) or (
        sequence_op is not None and sequence_op.value != initial_sequence + len(targets)
    ):
        _invalid("Area damage sequence differs from the resolved packets")
    if [event.target_id for event in deaths] != dead or any(e.reason != "damage" for e in deaths):
        _invalid("Area death evidence differs from resolved terminal damage")
    if (death_ledger is not None) != bool(dead):
        _invalid("Area deaths require one encounter death ledger update")
    if death_ledger is not None:
        old = death_ledger.expected_records
        records = death_ledger.records[len(old) :]
        if (
            death_ledger.dead_ids != death_ledger.expected_dead_ids | set(dead)
            or death_ledger.records[: len(old)] != old
            or [record.target_id for record in records] != dead
            or any(
                record.killer_id != caster
                or record.location_id != snapshot.scene_state.scene_id
                or record.target_kind != targets[record.target_id].entity_type.lower()
                or record.reason != "damage"
                for record in records
            )
        ):
            _invalid("Area death ledger differs from resolved terminal damage")
    return set(dead)
