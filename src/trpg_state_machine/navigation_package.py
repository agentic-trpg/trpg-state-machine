"""Package /3 knowledge grants: explicit public regions, never inferred memory."""

from .navigation_models import NavigationKnowledge, NavigationRegion


def validate_navigation(raw, scenes, passages, actors, templates):
    """Validate navigation knowledge and scene/passage references."""
    from .package import _fail, _identifier, _list, _mapping, _reference, _unique

    data = _mapping(raw, "regions actor_grants pc_template_grants", "navigation")
    by_scene = {s.scene_id: s for s in scenes}
    by_passage = {p.passage_id: p for p in passages}
    regions = []
    for value in _list(data["regions"], "navigation.regions"):
        value = _mapping(value, "region_id scene_ids passage_ids", "navigation.regions")
        rid = _identifier(value["region_id"], "navigation.region_id", "region")
        scene_ids = tuple(_list(value["scene_ids"], "navigation.scene_ids"))
        passage_ids = tuple(_list(value["passage_ids"], "navigation.passage_ids"))
        if not scene_ids or len(scene_ids) > 32 or len(passage_ids) > 64:
            _fail("schema_error", "navigation", "Bounded nonempty public regions required")
        for sid in scene_ids:
            _reference(sid, by_scene, "navigation.scene_ids")
            if not by_scene[sid].public_interactions or by_scene[sid].grid_json is None:
                _fail("unsupported_navigation", "navigation", "Public fine scenes only")
        for pid in passage_ids:
            _reference(pid, by_passage, "navigation.passage_ids")
            if not {e.scene_id for e in by_passage[pid].endpoints} <= set(scene_ids):
                _fail("invalid_reference", "navigation", "Route endpoints must belong to region")
        _unique(scene_ids, "navigation.scene_ids")
        _unique(passage_ids, "navigation.passage_ids")
        regions.append(NavigationRegion(rid, scene_ids, passage_ids))
    _unique([r.region_id for r in regions], "navigation.regions")
    if len(regions) > 32:
        _fail("schema_error", "navigation.regions", "At most 32 shared regions")
    region_ids = {r.region_id for r in regions}

    def grants(name, identity, valid):
        """Validate navigation grants on an actor or template."""
        result = []
        for grant in _list(data[name], "navigation." + name):
            grant = _mapping(grant, identity + " region_ids", "navigation." + name)
            _reference(grant[identity], valid, "navigation." + identity)
            selected = tuple(_list(grant["region_ids"], "navigation.region_ids"))
            for rid in selected:
                _reference(rid, region_ids, "navigation.region_ids")
            _unique(selected, "navigation.region_ids")
            result.append((grant[identity], selected))
        _unique([g[0] for g in result], "navigation." + name)
        return tuple(result)

    return NavigationKnowledge(
        tuple(regions),
        grants("actor_grants", "actor_id", {a.actor_id for a in actors}),
        grants("pc_template_grants", "pc_template_id", {p.pc_template_id for p in templates}),
    )
