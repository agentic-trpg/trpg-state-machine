"""Strict local npc-state/1 static templates; not a cross-module public ABI."""

from .npc_state import (
    NPC_STATE_SCHEMA,
    RECORD_TYPES,
    NPCInitialState,
    NPCProfile,
    NPCRuntimeState,
    ScheduleEntry,
    SourceReference,
    WorldFactDefinition,
)
from .package import _fail, _identifier, _list, _mapping, _reference, _text, _unique


def _bounded_list(value, path, limit=100):
    items = _list(value, path)
    if len(items) > limit:
        _fail("schema_error", path, f"Maximum {limit} entries")
    return items


def _integer(value, path, maximum=1_000_000):
    if type(value) is not int or not 0 <= value <= maximum:
        _fail("schema_error", path, f"Expected integer 0..{maximum}")
    return value


def _enum(value, allowed, path):
    if type(value) is not str or value not in allowed:
        _fail("schema_error", path, f"Expected one of {sorted(allowed)}")
    return value


def _texts(value, path):
    return tuple(_text(v, path) for v in _bounded_list(value, path, 16))


def parse_world_facts(raw, npc_ids):
    facts = []
    for i, value in enumerate(_bounded_list(raw, "world.world_facts")):
        path = f"world.world_facts[{i}]"
        data = _mapping(value, "fact_id text visibility authorized_npc_ids", path)
        ids = tuple(
            _identifier(v, path, "npc") for v in _bounded_list(data["authorized_npc_ids"], path)
        )
        _unique(ids, path)
        for npc in ids:
            _reference(npc, npc_ids, path)
        visibility = _enum(data["visibility"], {"npc_authorized", "dm_only"}, path)
        if visibility == "dm_only" and ids:
            _fail("invalid_reference", path, "DM-only facts cannot grant NPC access")
        facts.append(
            WorldFactDefinition(
                _identifier(data["fact_id"], path, "fact"),
                _text(data["text"], path),
                visibility,
                ids,
            )
        )
    _unique([f.fact_id for f in facts], "world.world_facts")
    return tuple(facts)


def parse_npc_state(raw, npc_id, npc_ids, scene_ids, facts):
    path = f"actors.{npc_id}.npc_state"
    data = _mapping(
        raw,
        "schema_version profile initial_knowledge_ids memories beliefs relationships "
        "goals plans runtime",
        path,
    )
    if data["schema_version"] != NPC_STATE_SCHEMA:
        _fail("unsupported_schema", path, f"Expected local {NPC_STATE_SCHEMA}")
    p = _mapping(data["profile"], "role personality speaking_style constraints", path + ".profile")
    profile = NPCProfile(
        _text(p["role"], path),
        _texts(p["personality"], path),
        _text(p["speaking_style"], path),
        _texts(p["constraints"], path),
    )
    known = tuple(
        _identifier(v, path, "fact") for v in _bounded_list(data["initial_knowledge_ids"], path)
    )
    _unique(known, path)
    for key in known:
        fact = next((f for f in facts if f.fact_id == key), None)
        if (
            fact is None
            or fact.visibility != "npc_authorized"
            or npc_id not in fact.authorized_npc_ids
        ):
            _fail(
                "invalid_reference", path, "Known Fact must be explicitly authorized for this NPC"
            )
    records = {}
    for kind, plural in (
        ("memory", "memories"),
        ("belief", "beliefs"),
        ("relationship", "relationships"),
        ("goal", "goals"),
        ("plan", "plans"),
    ):
        items = []
        for i, value in enumerate(_bounded_list(data[plural], path + "." + plural)):
            at = f"{path}.{plural}[{i}]"
            extra = {
                "memory": "epistemic_status",
                "belief": "epistemic_status confidence_basis_points status",
                "relationship": "target_id stance",
                "goal": "status",
                "plan": "goal_id goal_version steps status",
            }[kind]
            r = _mapping(value, "record_id version is_current text sources " + extra, at)
            rid = _identifier(r["record_id"], at, kind)
            version = _integer(r["version"], at)
            if version == 0 or type(r["is_current"]) is not bool:
                _fail("schema_error", at, "Positive version and Boolean is_current required")
            sources = []
            for value in _bounded_list(r["sources"], at + ".sources", 4):
                s = _mapping(value, "kind reference_id version", at + ".sources")
                source_kind = _enum(s["kind"], {"package", "memory"}, at)
                if source_kind == "package":
                    if s["reference_id"] != npc_id or s["version"] is not None:
                        _fail(
                            "invalid_reference",
                            at,
                            "Package source must be this NPC's approved template",
                        )
                else:
                    _identifier(s["reference_id"], at, "memory")
                    if _integer(s["version"], at) == 0:
                        _fail("invalid_reference", at, "Positive source memory version required")
                sources.append(SourceReference(source_kind, s["reference_id"], s["version"]))
            if not sources:
                _fail("invalid_reference", at, "At least one explicit source required")
            _unique(sources, at)
            args = dict(
                record_id=rid,
                version=version,
                is_current=r["is_current"],
                text=_text(r["text"], at),
                sources=tuple(sources),
            )
            if kind in ("memory", "belief"):
                args["epistemic_status"] = _enum(
                    r["epistemic_status"], {"observed", "inferred", "told"}, at
                )
            if kind == "belief":
                confidence = r["confidence_basis_points"]
                args["confidence_basis_points"] = (
                    None if confidence is None else _integer(confidence, at, 10_000)
                )
                args["status"] = _enum(r["status"], {"active", "retracted"}, at)
            if kind == "relationship":
                _reference(r["target_id"], npc_ids | {"selected_pc"}, at)
                if r["target_id"] == npc_id:
                    _fail("invalid_reference", at, "Relationship target must be another Actor")
                args["target_id"] = r["target_id"]
                args["stance"] = _enum(r["stance"], {"friendly", "neutral", "hostile"}, at)
            if kind in ("goal", "plan"):
                args["status"] = _enum(r["status"], {"active", "completed", "abandoned"}, at)
            if kind == "plan":
                args["goal_id"] = _identifier(r["goal_id"], at, "goal")
                args["goal_version"] = _integer(r["goal_version"], at)
                args["steps"] = _texts(r["steps"], at)
            items.append(RECORD_TYPES[kind](**args))
        _unique([(r.record_id, r.version) for r in items], path)
        for rid in {r.record_id for r in items}:
            history = sorted((r for r in items if r.record_id == rid), key=lambda r: r.version)
            if [r.version for r in history] != list(range(1, len(history) + 1)) or [
                r.is_current for r in history
            ] != [False] * (len(history) - 1) + [True]:
                _fail(
                    "schema_error",
                    path,
                    "History must be contiguous with exactly the latest version current",
                )
        records[kind] = tuple(sorted(items, key=lambda r: (r.record_id, r.version)))
    # Local references never resolve against another NPC or Session.
    memories = {(r.record_id, r.version): r for r in records["memory"]}
    for items in records.values():
        for r in items:
            for s in r.sources:
                if s.kind == "memory" and (s.reference_id, s.version) not in memories:
                    _fail("invalid_reference", path, "Source memory must belong to this NPC")
    # A stable ID is not a chronology. Check actual provenance cycles instead.
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            _fail("invalid_reference", path, "Circular memory provenance is unsupported")
        if key in visited:
            return
        visiting.add(key)
        for source in memories[key].sources:
            if source.kind == "memory":
                visit((source.reference_id, source.version))
        visiting.remove(key)
        visited.add(key)

    for key in memories:
        visit(key)
    goals = {(r.record_id, r.version) for r in records["goal"]}
    for plan in records["plan"]:
        if (plan.goal_id, plan.goal_version) not in goals:
            _fail("invalid_reference", path, "Plan goal must belong to this NPC")
    runtime = _mapping(data["runtime"], "schedule current_activity", path + ".runtime")
    schedule = []
    for value in _bounded_list(runtime["schedule"], path + ".runtime.schedule", 16):
        s = _mapping(value, "schedule_id scene_id start_tick end_tick activity", path)
        _reference(s["scene_id"], scene_ids, path)
        start, end = _integer(s["start_tick"], path), _integer(s["end_tick"], path)
        if start >= end:
            _fail("schema_error", path, "Schedule start must precede end; no execution implied")
        schedule.append(
            ScheduleEntry(
                _identifier(s["schedule_id"], path, "schedule"),
                s["scene_id"],
                start,
                end,
                _text(s["activity"], path),
            )
        )
    _unique([s.schedule_id for s in schedule], path)
    activity = runtime["current_activity"]
    if activity is not None:
        _text(activity, path)
    return NPCInitialState(
        profile,
        known,
        records["memory"],
        records["belief"],
        records["relationship"],
        records["goal"],
        records["plan"],
        NPCRuntimeState(tuple(schedule), activity),
    )
