"""Current target-local observer facts for exploration sight."""

from dnd5e_engine.visual_sight import SEE_INVISIBLE_KEY


def sight_conditions_unknown(characters, sid, actor_id, *, neutral_conditions=frozenset()):
    """Reject unreviewed sight conditions, with any caller-proven neutral conditions."""
    independent = characters.condition_names(sid, actor_id)
    if any(
        name not in {"blinded", "deafened", "invisible"} and name not in neutral_conditions
        for name in independent
    ):
        return True
    linked = characters.effects
    if not linked.has_linked_conditions(sid, actor_id, capability="observer_visibility"):
        return False
    # Keep the established RE proof for neutral linked region conditions and
    # reviewed Invisibility; only the newly supported Blinded gate needs more.
    return any(
        name not in {"blinded", "deafened"} and name not in neutral_conditions
        for name in linked.linked_condition_names(sid, actor_id)
    )


def observer_blinded(characters, sid, actor_id):
    """Read canonical Blinded from this actor's independent and linked conditions."""
    return "blinded" in characters.condition_names(
        sid, actor_id
    ) or "blinded" in characters.effects.linked_condition_names(sid, actor_id)


def can_see_invisible(characters, sid, actor_id):
    """Recognize only the typed enabled RE grant in this actor's current effects."""
    return any(
        not effect["disabled"]
        and any(
            change["key"] == SEE_INVISIBLE_KEY
            and change["mode"] == "override"
            and change["value"] is True
            for change in effect["changes"]
        )
        for effect in characters.effects.for_targets(sid, (actor_id,))
    )
