"""Read exact current creature sight facts for trusted exploration rule preparation."""

from .errors import StateMachineError
from .grid import cell_in_bounds
from .perception import sight_ray_occlusion_unresolved
from .spatial import SpatialPosition
from .visual_observer_state import CHECK_NEUTRAL_CONDITIONS, sight_conditions_unknown
from .visual_target_state import target_is_invisible


def read_creature_sight(world, sid, observer_id, source_ids, package):
    """Resolve supported sight relationships for an already authorized check observer.

    Omit unresolved sources, placements or sight profiles. The fixed facts may
    affect the rule roll and are never candidates for public target disclosure.
    The caller holds the same scope as the request's mechanical snapshot.
    """
    placement = world.facts.carrier_position(sid, observer_id)
    if placement is None:
        return {}
    position = SpatialPosition(*placement)
    scene_id = position.scene_id
    proof = world.store.connection.execute(
        "SELECT complete FROM scene_integrity WHERE session_id=? AND scene_id=?", (sid, scene_id)
    ).fetchone()
    if proof is None or not proof[0]:
        return {}
    definition = world.facts.instances.actor(sid, observer_id)
    if (
        definition and (definition.senses is None or not dict(definition.senses)["visual"])
    ) or _unknown_conditions(world, sid, observer_id):
        return {}
    targets = {}
    for identity in source_ids:
        target = world.facts.carrier_position(sid, identity)
        if (
            target is not None
            and target[0] == scene_id
            and target[1] is not None
            and not _unknown_conditions(world, sid, identity)
        ):
            targets[identity] = target[1]
    if not targets:
        return {}
    try:
        sight = world._prepare_visibility(
            sid, observer_id, position, package, cells=tuple(targets.values())
        )
    except StateMachineError as exc:
        if exc.code == "spatial_unconfigured":
            return {}
        raise
    if not cell_in_bounds(sight.topology, position.cell):
        return {}
    known = {}
    for identity, cell in targets.items():
        if not cell_in_bounds(sight.topology, cell):
            continue
        visible = sight.can_see_creature(cell, invisible=target_is_invisible(world, sid, identity))
        if not visible or not sight_ray_occlusion_unresolved(world, sid, position, cell, package):
            known[identity] = visible
    return known


def _unknown_conditions(world, sid, actor_id):
    """Allow reviewed Frightened and Poisoned without treating either as sight modifiers."""
    return sight_conditions_unknown(
        world.characters, sid, actor_id, neutral_conditions=CHECK_NEUTRAL_CONDITIONS
    )
