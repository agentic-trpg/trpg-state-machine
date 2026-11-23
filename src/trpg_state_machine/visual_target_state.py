"""Project a target's effective Invisible state from keyed current facts.

This is the shared sight rule input for actor and object IDs. Source-linked
suppression changes the benefit of Invisible without deleting its base fact.
"""

from typing import Literal


def target_is_invisible(
    world, sid: str, target_id: str, *, target_kind: Literal["creature", "object"] = "creature"
) -> bool:
    """Return whether this target currently benefits from Invisible.

    The caller establishes its authorized read scope. A target visual effect
    suppresses Invisible only during its active elapsed-time interval; source
    removal restores any independent or linked Invisible condition unchanged.
    Object base state comes from its authored instance fact.
    """
    db = world.store.connection
    if target_kind == "object":
        base_invisible = world.facts.object_invisible(sid, target_id)
    else:
        actor = db.execute(
            "SELECT 1 FROM mechanical_actors WHERE session_id=? AND actor_id=?",
            (sid, target_id),
        ).fetchone()
        if not actor:
            return False
        base_invisible = "invisible" in world.characters.condition_names(
            sid, target_id
        ) or "invisible" in world.characters.effects.linked_condition_names(sid, target_id)
    if not base_invisible:
        return False
    tick = world.facts.time_state(sid)[1]
    for (source_id,) in db.execute(
        "SELECT source_id FROM target_visual_effects "
        "WHERE session_id=? AND target_kind=? AND target_id=? AND suppresses_invisible=1",
        (sid, target_kind, target_id),
    ):
        source = db.execute(
            "SELECT expires_tick FROM visual_effect_sources WHERE session_id=? AND source_id=?",
            (sid, source_id),
        ).fetchone()
        if source is not None and source[0] > tick:
            return False
    return True
