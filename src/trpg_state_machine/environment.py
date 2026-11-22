"""Scene environment business and read-only derived queries over shared WorldFacts."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType

from dnd5e_engine.environment import StrongWind
from dnd5e_engine.evaluation_region_effect import validate_region_effect_rule
from dnd5e_engine.evaluation_ruleset import RulesetBinding, RulesetBindingError

from .environment_commands import (
    AddEnvironmentSource,
    ApplyStrongWind,
    BatchEnvironment,
    InitializeEnvironment,
    RemoveEnvironmentSource,
    ReplaceEnvironmentSource,
    SetEnvironmentBase,
    SetEnvironmentProtection,
    validate_environment_operation,
)
from .environment_models import (
    CONDITION_TYPES,
    MAX_CELLS,
    EnvironmentArea,
    EnvironmentEffectsProvider,
    EnvironmentSceneFacts,
    HeightInterval,
    Obscurant,
    Precipitation,
    Temperature,
    UnitHeightProvider,
    Wind,
    finite,
    invalid,
    region_rule_input,
    validate_sources,
)
from .environment_versions import EnvironmentVersions
from .errors import StateMachineError
from .grid import cell_in_bounds
from .object_protection import PRECIPITATION_KINDS
from .runtime_instances import SceneFieldCache, resolve_field
from .session_access import _binding


@dataclass(frozen=True, slots=True)
class ConditionContribution:
    """Attribute a condition value to its source, exposure and observability."""

    source_ref: str
    value: Temperature | Wind | Precipitation | Obscurant
    exposure: str
    observable: bool


@dataclass(frozen=True, slots=True)
class ResolvedCondition:
    """Represent resolved condition status with its contributing evidence."""

    status: str
    contributions: tuple[ConditionContribution, ...] = ()


@dataclass(frozen=True, slots=True)
class ProtectionBand:
    """Associate a protection source with its vertical interval."""

    source_ref: str
    heights: HeightInterval


@dataclass(frozen=True, slots=True)
class CellProtection:
    """Collect wind and precipitation protection evidence, preserving unknown statuses."""

    rain_status: str
    rain_sources: tuple[str, ...]
    wind_status: str
    wind_bands: tuple[ProtectionBand, ...]
    precipitation: tuple[tuple[str, str, tuple[str, ...]], ...] = ()

    def for_precipitation(self, kind):
        """Select the protection state and sources for the precipitation kind."""
        if kind == "rain":
            return self.rain_status, self.rain_sources
        return next(
            ((status, refs) for k, status, refs in self.precipitation if k == kind), ("unknown", ())
        )


@dataclass(frozen=True, slots=True)
class CellEnvironment:
    """Hold one cell/height's composed environment and exposure result."""

    scene_id: str
    cell: str
    height_ft: float | None
    temperature: ResolvedCondition
    wind: ResolvedCondition
    precipitation: ResolvedCondition
    obscurant: ResolvedCondition
    ground_precipitation: ResolvedCondition
    wind_exposure: ResolvedCondition
    protection: CellProtection
    effects_status: str


@dataclass(frozen=True, slots=True)
class EnvironmentHeightSegment:
    """Associate a vertical interval with its composed environment."""

    heights: HeightInterval
    environment: CellEnvironment


@dataclass(frozen=True, slots=True)
class UnitEnvironment:
    """Represent unit exposure readiness and its resolved height segments."""

    status: str
    segments: tuple[EnvironmentHeightSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class _RegionConditionIndex:
    """Retain immutable source and rule pairs for configured cells of one Scene."""

    by_cell: Mapping[str, tuple[tuple[str, str], ...]]
    combat_by_cell: Mapping[str, tuple[tuple[str, str], ...]]

    def __deepcopy__(self, memo):
        """Share the immutable buckets with candidate and fixed read scopes."""
        return self


def _value_status(value):
    """Classify an environment value as known, absent or unknown."""
    if type(value) is Temperature:
        return "unknown" if value.celsius is None else "ready"
    if type(value) is Wind:
        if value.speed_mph == 0:
            return "none"
        return "unknown" if value.speed_mph is None or value.wind_from is None else "ready"
    kind = value.kind
    density = value.intensity if type(value) is Precipitation else value.density
    if "unknown" in (kind, density):
        return "unknown"
    return "none" if kind == "none" else "ready"


def _resolve(candidates, *, layers=False):
    """Resolve contributions by priority while preserving unknown and unsupported states."""
    if not candidates:
        return ResolvedCondition("unconfigured")
    if not layers:
        priority = max(p for p, _ in candidates)
        candidates = [(p, c) for p, c in candidates if p == priority]
    values = tuple(c for _, c in sorted(candidates, key=lambda pair: pair[1].source_ref))
    if not layers and any(c.value != values[0].value for c in values):
        return ResolvedCondition("unsupported", values)
    statuses = {_value_status(c.value) for c in values}
    status = "unknown" if "unknown" in statuses else "ready" if "ready" in statuses else "none"
    return ResolvedCondition(status, values)


class EnvironmentService:
    """Compose scoped environment fields from world facts and versioned providers."""

    def __init__(
        self,
        handler,
        *,
        command_submitter=None,
        effects_provider: EnvironmentEffectsProvider | None = None,
        unit_height_provider: UnitHeightProvider | None = None,
    ):
        """Bind environment providers, dual input versions and bounded field caches."""
        self.h = handler
        self.command_submitter = command_submitter
        self.effects_provider = effects_provider
        self.unit_height_provider = unit_height_provider
        self._projections = SceneFieldCache(handler.store, "protection", 32)
        self._cells = SceneFieldCache(handler.store, "cells", 128)
        self.versions = EnvironmentVersions(handler.store)

    def clear_snapshots(self):
        """Disposable derived state only; also cancel publication of pending old entries."""
        self._projections.clear()
        self._cells.clear()

    def release_session(self, sid):
        """Release retained environment fields for the Session."""
        self._projections.release(sid)
        self._cells.release(sid)

    def bind_providers(self, sid, package, *, context):
        """Trusted host configuration; register owner dependencies atomically, not on reads."""
        with self.h._transaction(write=True):
            self.h._session(sid, package, context)
            if not context.can_manage_environment:
                raise StateMachineError("unauthorized", "Environment management grant required")
            for scene in self.h.facts.instances.scenes(sid):
                for role, provider in (
                    ("effects", self.effects_provider),
                    ("protection", self.h.objects.spatial.protection_provider),
                ):
                    if self.versions.descriptor(provider) is not None:
                        self.versions.bind(sid, scene.scene_id, package, provider, role)

    def _query_cache(self, sid, scene, package):
        """Determine cache eligibility from bindings, provider identity and
        source/protection versions.
        """
        versions = self.versions.current(sid, scene)
        cacheable, providers = self.versions.cacheable(
            sid, scene, self.effects_provider, self.h.objects.spatial.protection_provider
        )
        return {
            "identity": (
                sid,
                package.adventure_id,
                package.package_version,
                package.package_digest,
                package.ruleset_binding,
                scene,
                providers,
            ),
            "versions": versions,
            "scope": self.h.store.derived_read_scope if cacheable else None,
            "valid": lambda: self.versions.current(sid, scene) == versions,
            "protection_valid": lambda: self.versions.current(sid, scene)[1] == versions[1],
        }

    def _prepared_protection(self, sid, scene, package, cache):
        """Prepare scene protection once per batch, omitting explicitly empty
        contributions.
        """
        if "inputs" not in cache:
            prepared = self.h.objects.spatial.prepare_scene_protection(sid, scene, package)
            cache["inputs"] = replace(
                prepared, objects=tuple(p for p in prepared.objects if not p.explicitly_empty())
            )
        return cache["inputs"]

    def _environment_inputs(self, sid, scene, package, cache, public):
        """Reuse scoped environment inputs and apply disclosure policy for public requests."""
        if "environment" not in cache:
            cache["environment"] = (
                self._inputs(sid, scene, package)
                if cache["scope"] is None
                else resolve_field(
                    self.h.store,
                    "scene",
                    sid,
                    scene,
                    "environment",
                    "inputs",
                    (cache["identity"], cache["versions"]),
                    lambda: self._inputs(sid, scene, package),
                    capacity=4,
                    versioned_parameters=True,
                )
            )
        if public:
            return self._disclosure_inputs(cache["environment"])
        return cache["environment"], frozenset()

    def initialize(self, sid, seeds, package, *, command_id, expected_world_version, context):
        """Real controlled import; same command, grants, OCC and receipts as every write."""
        from .commands import TypedCommand

        with self.h._transaction():
            row = self.h._session(sid, package, context)
            actor = row["pc_actor_id"]
        if type(seeds) is not tuple or any(type(s) is not InitializeEnvironment for s in seeds):
            invalid("Initialize requires immutable typed Scene seeds")
        if self.command_submitter is None:
            raise StateMachineError(
                "unsupported_environment_initialization", "CommandRuntime adapter is required"
            )
        return self.command_submitter(
            TypedCommand(
                sid,
                command_id,
                actor,
                "world.batch_environment",
                BatchEnvironment(seeds),
                expected_world_version,
            ),
            package,
            context=context,
        )

    def _area(self, sid, scene_id, area, package, *, query=False):
        """Validate scene/area bounds, enumerating cells only for an explicit full-scene
        request.
        """
        if type(area) is not EnvironmentArea:
            invalid("Expected typed area")
        if not self.h.facts.instances.has_scene(sid, scene_id):
            raise StateMachineError("invalid_target", "Unknown environment Scene")
        if area.cells:
            topology = self.h.grid.reader.prepared_scene(sid, scene_id, package).topology
            if any(not cell_in_bounds(topology, c) for c in area.cells):
                raise StateMachineError("invalid_cell", "Environment area exceeds Scene bounds")
            return area.cells
        if query:
            grid = self.h.grid.reader.prepared_scene(sid, scene_id, package)
            width, height = grid.data["width"], grid.data["height"]
            if width * height > MAX_CELLS:
                invalid("Whole Scene query exceeds 256 cells; choose a bounded area")
            return tuple(f"{x},{y}" for x in range(width) for y in range(height))
        return ()

    def _validate_facts(self, sid, scene, facts, package):
        """Validate the areas referenced by environment sources."""
        self._area(sid, scene, EnvironmentArea(), package)
        for source in facts.sources:
            self._area(sid, scene, source.area, package)
        if facts.protection.rain_cells:
            self._area(sid, scene, EnvironmentArea(facts.protection.rain_cells), package)
        for protection in facts.protection.precipitation or ():
            self._area(sid, scene, EnvironmentArea(protection.cells), package)
        for wind in facts.protection.wind or ():
            self._area(sid, scene, EnvironmentArea((wind.cell,)), package)

    def _admit_region_rule(self, row, rule_ref):
        """Validate an authored region effect against this Session's frozen content."""
        if rule_ref is None:
            return
        try:
            validate_region_effect_rule(
                region_rule_input(rule_ref),
                ruleset_binding=RulesetBinding(**asdict(_binding(row))),
                loader=self.h.loader,
            )
        except RulesetBindingError as exc:
            raise StateMachineError("ruleset_binding_mismatch", str(exc)) from exc
        except ValueError as exc:
            invalid(str(exc))

    def operate(self, row, op, package):
        """Plan environment writes and settle runtime region changes in one candidate."""
        self.h.store.require_write()
        validate_environment_operation(op)
        sid = row["session_id"]
        if type(op) is ApplyStrongWind:
            if self.h._active_combat(sid):
                raise StateMachineError(
                    "unsupported_combat_world_operation", "Strong wind requires exploration"
                )
            if op.scene_id != row["current_scene_id"]:
                raise StateMachineError("invalid_target", "Strong wind requires the current Scene")
            return self.h.magic.apply_strong_wind(
                sid, op.scene_id, package, StrongWind(source_id=op.source_id, cells=op.cells)
            )
        operations = op.changes if type(op) is BatchEnvironment else (op,)
        originals, planned, changes = {}, {}, []
        regions = defaultdict(dict)
        for change in operations:
            scene = change.scene_id
            self._area(sid, scene, EnvironmentArea(), package)
            if scene not in planned:
                originals[scene] = self.h.facts.environment(sid, scene)
                planned[scene] = originals[scene]
            old = planned[scene]
            facts = old or EnvironmentSceneFacts()
            source_id, areas, changed_fields = None, [], []
            if type(change) is InitializeEnvironment:
                self._validate_facts(sid, scene, change.facts, package)
                for rule_ref in dict.fromkeys(
                    source.region_rule_ref for source in change.facts.sources
                ):
                    self._admit_region_rule(row, rule_ref)
                new = old if old is not None else change.facts
                areas = [EnvironmentArea()]
                changed_fields = ["initialization", *CONDITION_TYPES, "protection"]
            elif type(change) is SetEnvironmentBase:
                new = replace(facts, base=change.conditions)
                areas = [EnvironmentArea()]
                changed_fields = [
                    k
                    for k in CONDITION_TYPES
                    if getattr(facts.base, k, None) != getattr(change.conditions, k)
                ]
                if facts.base is None:
                    changed_fields.append("base_configuration")
            elif type(change) is SetEnvironmentProtection:
                new = replace(facts, protection=change.protection)
                areas = [EnvironmentArea()]
                changed_fields = ["protection"]
            else:
                source_id = (
                    change.source_id
                    if type(change) is RemoveEnvironmentSource
                    else change.source.source_id
                )
                sources = {s.source_id: s for s in facts.sources}
                before = sources.get(source_id)
                if type(change) is AddEnvironmentSource:
                    if before is not None and before != change.source:
                        raise StateMachineError(
                            "environment_source_exists", "Source already exists"
                        )
                    self._admit_region_rule(row, change.source.region_rule_ref)
                    sources[source_id] = change.source
                elif type(change) is ReplaceEnvironmentSource:
                    if before is None:
                        raise StateMachineError("environment_source_not_found", "Source is absent")
                    rule_ref = change.source.region_rule_ref
                    if (
                        before.combat_region != change.source.combat_region
                        or before.region_rule_ref != rule_ref
                        or (before.combat_region is not None and before.area != change.source.area)
                    ):
                        invalid("Replace cannot change the region rule or combat coverage")
                    self._admit_region_rule(row, rule_ref)
                    sources[source_id] = change.source
                else:
                    if before is not None and before.combat_region is not None:
                        invalid("Remove cannot remove a combat environment region")
                    sources.pop(source_id, None)
                after = sources.get(source_id)
                if before == after:
                    continue
                region = before or after
                if region.region_rule_ref is not None and region.combat_region is None:
                    first, _ = regions[scene].get(source_id, (before, None))
                    regions[scene][source_id] = first, after
                areas = [s.area for s in (before, after) if s is not None]
                changed_fields = [
                    k
                    for k in CONDITION_TYPES
                    if getattr(getattr(before, "conditions", None), k, None)
                    != getattr(getattr(after, "conditions", None), k, None)
                ]
                if before is None or after is None:
                    changed_fields.append("source_presence")
                else:
                    changed_fields += [
                        k
                        for k in ("area", "priority", "exposure", "observable")
                        if getattr(before, k) != getattr(after, k)
                    ]
                new = replace(facts, sources=tuple(sources[k] for k in sorted(sources)))
            self._validate_facts(sid, scene, new, package)
            if new == old:
                continue
            planned[scene] = new
            changes.append(
                {
                    "scene_id": scene,
                    "operation": type(change).__name__,
                    "source_id": source_id,
                    "areas": [asdict(a) for a in areas],
                    "fields": changed_fields,
                }
            )
        changed_scenes = sorted(s for s in planned if planned[s] != originals[s])
        if not changed_scenes:
            return []
        for scene in changed_scenes:
            self.h.facts.set_environment(sid, scene, planned[scene])
            if scene in regions:
                self._apply_region_coverage_changes(row, scene, regions[scene])
        return [
            self.h._capture_event(
                sid,
                package,
                "environment.changed",
                {
                    "scene_ids": changed_scenes,
                    "changes": [c for c in changes if c["scene_id"] in changed_scenes],
                },
                phase="environment.after_batch",
            )
        ]

    def _apply_region_coverage_changes(self, row, scene, sources):
        """Reconcile net footprint changes against actor positions in the write candidate.

        Shared coverage with the same rule keeps the existing effect. The grid reader
        finds only occupants of added or removed cells, and Effects owns each edit.
        """
        entered, left = defaultdict(set), defaultdict(set)
        for source_id, (before, after) in sources.items():
            old_rule = before.region_rule_ref if before is not None else None
            new_rule = after.region_rule_ref if after is not None else None
            previous = set(before.area.cells) if old_rule is not None else set()
            current = set(after.area.cells) if new_rule is not None else set()
            retained = previous & current if old_rule == new_rule else set()
            for cell in previous - retained:
                left[cell].add((scene, source_id, old_rule))
            for cell in current - retained:
                entered[cell].add((scene, source_id, new_rule))

        affected = entered.keys() | left.keys()
        if not affected:
            return
        actors, _ = self.h.grid.reader.region_occupants(
            row["session_id"], scene, affected, include_actors=True, include_objects=False
        )
        for actor, cell in sorted(map(tuple, actors)):
            self.h.scenes.apply_region_membership_changes(
                row,
                actor,
                entered=entered.get(cell, ()),
                left=left.get(cell, ()),
                loader=self.h.loader,
            )

    def read_facts(self, sid, scene_id, package, *, context):
        """GM import/audit API, never a player source catalogue."""
        with self.h._transaction():
            self.h._session(sid, package, context)
            if not context.can_manage_environment:
                raise StateMachineError("unauthorized", "Environment management grant required")
            self._area(sid, scene_id, EnvironmentArea(), package)
            return self.h.facts.environment(sid, scene_id)

    def _region_condition_index(self, sid, scene_id):
        """Build cell associations from persisted fixed effects in source ID order."""
        facts = self.h.facts.environment(sid, scene_id)
        by_cell = {}
        combat_by_cell = {}
        for source in facts.sources if facts is not None else ():
            if (rule_ref := source.region_rule_ref) is not None:
                reference = (source.source_id, rule_ref)
                for cell in source.area.cells:
                    by_cell.setdefault(cell, []).append(reference)
                    if source.combat_region is not None:
                        combat_by_cell.setdefault(cell, []).append(reference)
        return _RegionConditionIndex(
            MappingProxyType({cell: tuple(references) for cell, references in by_cell.items()}),
            MappingProxyType(
                {cell: tuple(references) for cell, references in combat_by_cell.items()}
            ),
        )

    def region_condition_sources(
        self, sid: str, scene_id: str, cell: str | None, *, combat: bool = False
    ) -> tuple[tuple[str, str], ...]:
        """Return (source ID, rule reference) pairs for a trusted movement position.

        A position without a Grid cell has no regional membership. The bounded Scene
        field shares the authoritative facts' dependency in the candidate or read scope.
        Combat mode returns only sources whose coverage opts into the combat boundary.
        """
        if cell is None:
            return ()
        index = resolve_field(
            self.h.store,
            "scene",
            sid,
            scene_id,
            "environment",
            "region_conditions",
            (),
            lambda: self._region_condition_index(sid, scene_id),
        )
        return (index.combat_by_cell if combat else index.by_cell).get(cell, ())

    def _inputs(self, sid, scene, package):
        """Combine persisted environment facts, Effects sources and provider status."""
        facts = self.h.facts.environment(sid, scene) or EnvironmentSceneFacts()
        effects = (
            self.effects_provider.environment_sources(sid, scene, package)
            if self.effects_provider is not None
            else None
        )
        status = "unconfigured" if effects is None else "ready"
        effects = () if effects is None else effects
        validate_sources(effects)
        for source in effects:
            self._area(sid, scene, source.area, package)
        return facts, effects, status

    @staticmethod
    def _sources_by_cell(facts, effects, cells):
        """Bucket scene and Effects sources by their overlap with requested cache misses."""
        targets = set(cells)
        by_cell = {cell: [] for cell in cells}
        for prefix, sources in (("fact:", facts.sources), ("effect:", effects)):
            for source in sources:
                covered = targets.intersection(source.area.cells) if source.area.cells else cells
                entry = (prefix + source.source_id, source)
                for cell in covered:
                    by_cell[cell].append(entry)
        return by_cell

    @staticmethod
    def _conditions(base, sources, height):
        """Prioritize the sources already matched to a cell at the requested height."""
        candidates = {k: [] for k in CONDITION_TYPES}
        if base is not None:
            for k in candidates:
                value = getattr(base, k)
                if value is not None:
                    candidates[k].append(
                        (-1001, ConditionContribution("base", value, "outdoor", True))
                    )
        for ref, source in sources:
            heights = source.area.heights
            if (
                heights
                and height is not None
                and not (heights.lower_ft <= height < heights.upper_ft)
            ):
                continue
            for k, cls in CONDITION_TYPES.items():
                value = getattr(source.conditions, k)
                if value is None:
                    continue
                if heights and height is None:
                    value = cls()  # Applicable vertical scope is unknown, never guessed.
                candidates[k].append(
                    (
                        source.priority,
                        ConditionContribution(ref, value, source.exposure, source.observable),
                    )
                )
        return {k: _resolve(v, layers=k == "obscurant") for k, v in candidates.items()}

    def _direction_projection(self, sid, scene, package, direction, cache):
        """Reuse the scene's protection projection for one incoming wind direction."""
        if direction not in cache:
            cache[direction] = self._projections.resolve(
                cache["scope"],
                (cache["identity"], (cache["versions"][1],), direction),
                lambda: self.h.objects.spatial.scene_protection_contributions(
                    sid,
                    scene,
                    package,
                    wind_from=direction,
                    prepared=self._prepared_protection(sid, scene, package, cache),
                ),
                validator=cache["protection_valid"],
            )
        return cache[direction]

    def _protection_by_cell(self, sid, scene, package, static, cells_by_direction, cache):
        """Bucket projected coverage by target cell and retain scene-wide completeness.

        Object coverage can reach targets from anchors outside the requested area.
        Unknown provider inputs still apply to every target without known coverage.
        """
        cells = {
            cell: direction for direction, group in cells_by_direction.items() for cell in group
        }
        refs = defaultdict(set)
        bands = {cell: [] for cell in cells}
        for cell in static.rain_cells or ():
            if cell in cells:
                refs[cell, "rain"].add("static")
        for contribution in static.precipitation or ():
            for cell in contribution.cells:
                if cell in cells:
                    for kind in contribution.kinds:
                        refs[cell, kind].add("static")
        for band in static.wind or ():
            if cells.get(band.cell) == band.wind_from:
                bands[band.cell].append(ProtectionBand("static", band.heights))

        # Unknown and north share a projection, but only known wind uses its wind bands.
        by_projection = defaultdict(set)
        for direction, targets in cells_by_direction.items():
            by_projection[direction or "north"].update(targets)
        result = {}
        for key, targets in by_projection.items():
            objects = self._direction_projection(sid, scene, package, key, cache).objects
            has_wind = key in cells_by_direction
            incomplete = (
                set(PRECIPITATION_KINDS)
                if static.rain_cells is None and static.precipitation is None
                else set()
            )
            wind_complete = static.wind is not None
            for obj in objects:
                ref = "object:" + obj.object_id
                for c in obj.rain_cells:
                    if c.cell in targets:
                        refs[c.cell, "rain"].add(ref)
                for c in obj.precipitation_cells:
                    if c.cell in targets and c.kind != "rain":
                        refs[c.cell, c.kind].add(ref)
                if has_wind:
                    for c in obj.wind_cells:
                        if cells.get(c.cell) == key:
                            bands[c.cell].append(
                                ProtectionBand(
                                    ref, HeightInterval(c.lower_height_ft, c.upper_height_ft)
                                )
                            )
                    wind_complete = wind_complete and obj.wind_status in ("none", "ready")
                incomplete.update(
                    kind
                    for kind in PRECIPITATION_KINDS
                    if kind not in incomplete
                    and obj.precipitation_status(kind) not in ("none", "ready")
                )
            for cell in targets:
                if len(bands[cell]) > 4096:
                    invalid("Protection query exceeds 4096 source bands")
                precipitation = []
                for kind in PRECIPITATION_KINDS:
                    sources = tuple(sorted(refs.get((cell, kind), ())))
                    status = "protected" if sources else "unknown" if kind in incomplete else "none"
                    precipitation.append((kind, status, sources))
                _, rain_status, rain = precipitation[0]
                wind_status = (
                    "missing_input"
                    if cells[cell] is None
                    else "none"
                    if wind_complete
                    else "unknown"
                )
                result[cell] = CellProtection(
                    rain_status, rain, wind_status, tuple(bands[cell]), tuple(precipitation)
                )
        return result

    @staticmethod
    def _exposure(condition, protection_status, covered, *, wind=False, protection=None):
        """Derive effective wind and precipitation from protection and exposure semantics."""
        if condition.status not in ("ready", "none"):
            return condition
        result, uncertain = [], False
        for contribution in condition.contributions:
            value = contribution.value
            applicable = contribution.exposure == "outdoor" and _value_status(value) == "ready"
            status, is_covered = protection_status, covered
            if not wind and protection is not None:
                status, refs = protection.for_precipitation(value.kind)
                is_covered = bool(refs)
            if applicable and is_covered:
                value = Wind(0) if wind else Precipitation("none", "none")
            elif applicable and status != "none":
                uncertain = True
            result.append(replace(contribution, value=value))
        if uncertain:
            return ResolvedCondition("unknown", tuple(result))
        # Equal atmospheric values with different shelter policies can diverge locally.
        # Keep both; selecting one would invent a combination rule.
        return _resolve([(0, c) for c in result])

    def _area_cells(self, sid, scene, cells, package, height, *, public, validated=False):
        """Reuse valid cell results, preparing contributions only for misses in this scope."""
        cache = self._query_cache(sid, scene, package)
        parameters = tuple(("cell", public, cell, height) for cell in cells)

        def compute_missing(missing):
            """Resolve the missed cells with one shared source and shelter preparation."""
            targets = tuple(parameter[2] for parameter in missing)
            if not validated:
                self._area(sid, scene, EnvironmentArea(targets), package)
            (facts, effects, effects_status), hidden = self._environment_inputs(
                sid, scene, package, cache, public
            )
            sources = self._sources_by_cell(facts, effects, targets)
            conditions = {
                cell: self._conditions(facts.base, sources[cell], height) for cell in targets
            }
            cells_by_direction = defaultdict(list)
            for cell in targets:
                cells_by_direction[self._wind_direction(conditions[cell])].append(cell)
            protection = self._protection_by_cell(
                sid, scene, package, facts.protection, cells_by_direction, cache
            )
            results = {}
            for parameter, cell in zip(missing, targets, strict=True):
                # Ground exposure is evaluated without the requested ambient altitude.
                ground = (
                    conditions[cell]["precipitation"]
                    if height is None
                    else self._conditions(facts.base, sources[cell], None)["precipitation"]
                )
                result = self._compose_cell(
                    scene, cell, height, conditions[cell], ground, protection[cell], effects_status
                )
                results[parameter] = self._public(result, hidden) if public else result
            return results

        results = self._cells.resolve_cells(
            cache["scope"],
            cache["identity"],
            cache["versions"],
            parameters,
            compute_missing,
        )
        return tuple(results[parameter] for parameter in parameters)

    @staticmethod
    def _wind_direction(conditions):
        """Return the incoming wind direction if the condition resolved as ready."""
        wind = conditions["wind"]
        return wind.contributions[0].value.wind_from if wind.status == "ready" else None

    def _compose_cell(self, scene, cell, height, conditions, ground, protection, effects_status):
        """Combine prepared conditions and coverage into one cell's exposure result."""
        covered = height is not None and any(
            b.heights.lower_ft <= height < b.heights.upper_ft for b in protection.wind_bands
        )
        return CellEnvironment(
            scene,
            cell,
            height,
            **conditions,
            ground_precipitation=self._exposure(
                ground,
                protection.rain_status,
                bool(protection.rain_sources),
                protection=protection,
            ),
            wind_exposure=self._exposure(
                conditions["wind"],
                protection.wind_status if height is not None else "missing_input",
                covered,
                wind=True,
            ),
            protection=protection,
            effects_status=effects_status,
        )

    def project_area(self, sid, scene_id, area, package, *, height_ft=None):
        """Compose trusted point results in one read scope. Caller checks binding and grants."""
        if height_ft is not None:
            finite(height_ft)
        if type(area) is not EnvironmentArea or area.heights is not None:
            invalid("Area point query takes separate explicit height_ft")
        with self.h.grid.reader.read_scope():
            cells = area.cells or self._area(sid, scene_id, area, package, query=True)
            return self._area_cells(
                sid, scene_id, cells, package, height_ft, public=False, validated=not area.cells
            )

    def _range(self, sid, scene, cell, package, heights, inputs, cache):
        """Split the height range at source boundaries and compose each interval."""
        if type(heights) is not HeightInterval:
            invalid("Expected scene-datum height interval")
        self._area(sid, scene, EnvironmentArea((cell,)), package)
        facts, effects, effects_status = inputs
        sources = self._sources_by_cell(facts, effects, (cell,))[cell]
        points = {heights.lower_ft, heights.upper_ft}
        for _, source in sources:
            if source.area.heights:
                points.update((source.area.heights.lower_ft, source.area.heights.upper_ft))
        # Wind may change with height. Collect all four directions, retaining every gap.
        projections = {}
        for direction in ("north", "east", "south", "west"):
            projection = self._protection_by_cell(
                sid, scene, package, facts.protection, {direction: (cell,)}, cache
            )[cell]
            projections[direction] = projection
            for band in projection.wind_bands:
                points.update((band.heights.lower_ft, band.heights.upper_ft))
        points = sorted(p for p in points if heights.lower_ft <= p <= heights.upper_ft)
        if len(points) > 512:
            invalid("Height query exceeds 512 boundaries")
        ground = self._conditions(facts.base, sources, None)["precipitation"]
        segments = []
        for a, b in zip(points, points[1:], strict=False):
            conditions = self._conditions(facts.base, sources, a)
            direction = self._wind_direction(conditions)
            if direction not in projections:
                north = projections["north"]
                projections[direction] = replace(north, wind_status="missing_input", wind_bands=())
            segments.append(
                EnvironmentHeightSegment(
                    HeightInterval(a, b),
                    self._compose_cell(
                        scene, cell, a, conditions, ground, projections[direction], effects_status
                    ),
                )
            )
        return tuple(segments)

    def _cached_range(self, sid, scene, cell, heights, package, *, public=False):
        """Reuse height-segmented environment by versions, interval and public/private
        scope.
        """
        if type(heights) is not HeightInterval:
            invalid("Expected scene-datum height interval")
        EnvironmentArea((cell,))
        cache = self._query_cache(sid, scene, package)

        def compute():
            """Compose height segments and merge equal adjacent public results."""
            inputs, hidden = self._environment_inputs(sid, scene, package, cache, public)
            segments = self._range(sid, scene, cell, package, heights, inputs, cache)
            if not public:
                return segments
            segments = tuple(
                replace(s, environment=self._public(s.environment, hidden)) for s in segments
            )
            merged = []
            for segment in segments:
                if merged and replace(merged[-1].environment, height_ft=None) == replace(
                    segment.environment, height_ft=None
                ):
                    previous = merged[-1]
                    merged[-1] = replace(
                        previous,
                        heights=HeightInterval(previous.heights.lower_ft, segment.heights.upper_ft),
                    )
                else:
                    merged.append(segment)
            return tuple(merged)

        return self._cells.resolve(
            cache["scope"],
            (cache["identity"], cache["versions"], ("range", public, cell, heights)),
            compute,
            validator=cache["valid"],
        )

    def project_height_range(self, sid, scene_id, cell, heights, package):
        """INTERNAL trusted interval composition; uses the same revision entry point."""
        with self.h.grid.reader.read_scope():
            return self._cached_range(sid, scene_id, cell, heights, package)

    @staticmethod
    def _disclosure_inputs(inputs):
        """One Scene/field policy, independent of query Cell, height or priority.

        Exclude hidden sources from PUBLIC partition construction, then mask every
        field they might affect across the entire Scene. This is not a substitute
        base-weather answer: those fields (including dependent values) are unknown.
        Internal projections keep all true sources and their original geometry.
        """
        facts, effects, status = inputs
        hidden_fields = frozenset(
            field
            for source in (*facts.sources, *effects)
            if not source.observable
            for field in CONDITION_TYPES
            if getattr(source.conditions, field) is not None
        )
        public_inputs = (
            replace(facts, sources=tuple(s for s in facts.sources if s.observable)),
            tuple(s for s in effects if s.observable),
            status,
        )
        return public_inputs, hidden_fields

    @staticmethod
    def _public(result, hidden_fields):
        """Redact hidden fields and protection sources from the public environment result."""

        def redact(condition):
            """Remove source identities and exposure details from one condition
            contribution.
            """
            return replace(
                condition,
                contributions=tuple(
                    replace(c, source_ref="", exposure="", observable=True)
                    for c in condition.contributions
                ),
            )

        masked = set(hidden_fields)
        if "wind" in masked:
            masked.add("wind_exposure")
        if "precipitation" in masked:
            masked.add("ground_precipitation")
        return replace(
            result,
            **{
                k: ResolvedCondition("unknown") if k in masked else redact(getattr(result, k))
                for k in (*CONDITION_TYPES, "ground_precipitation", "wind_exposure")
            },
            protection=replace(
                result.protection,
                rain_sources=(),
                wind_bands=(),
                precipitation=tuple(
                    (k, "unknown" if "precipitation" in masked else status, ())
                    for k, status, _ in result.protection.precipitation
                ),
                rain_status="unknown"
                if "precipitation" in masked
                else result.protection.rain_status,
                wind_status="unknown" if "wind" in masked else result.protection.wind_status,
            ),
        )

    def query_area(self, sid, actor_id, scene_id, area, package, *, context, height_ft=None):
        """Authorize scene access and cell visibility before returning the requested public
        environment.
        """
        if height_ft is not None:
            finite(height_ft)
        if type(area) is not EnvironmentArea or area.heights is not None:
            invalid("Area point query takes separate explicit height_ft")
        with self.h._transaction():
            position = self.h._spatial_reader(sid, actor_id, package, context)
            if position.scene_id != scene_id:
                raise StateMachineError("unauthorized", "Only Actor's current Scene is authorized")
            cells = area.cells
            if cells:
                visibility = self.h._prepare_visible_targets(sid, actor_id, package, cells=cells)
            else:
                position = self.h._public_scope(sid, actor_id, package)
                _, (width, height) = self.h.grid.base_topology(sid, scene_id, with_extent=True)
                if width * height > MAX_CELLS:
                    invalid("Whole Scene query exceeds 256 cells; choose a bounded area")
                cells = tuple(f"{x},{y}" for x in range(width) for y in range(height))
                visibility = self.h._prepare_visibility(
                    sid, actor_id, position, package, cells=cells
                )
            for cell in cells:
                if not cell_in_bounds(visibility.topology, cell):
                    raise StateMachineError("invalid_cell", "Environment area exceeds Scene bounds")
                self.h._visible_target(sid, actor_id, cell, package, prepared=visibility)
            return self._area_cells(
                sid, scene_id, cells, package, height_ft, public=True, validated=True
            )

    def query_height_range(self, sid, actor_id, scene_id, cell, heights, package, *, context):
        """Authorize position/visibility before reading public environment over a height
        interval.
        """
        with self.h._transaction():
            position = self.h._spatial_reader(sid, actor_id, package, context)
            if position.scene_id != scene_id:
                raise StateMachineError("unauthorized", "Only Actor's current Scene is authorized")
            self.h._visible_target(sid, actor_id, cell, package)
            return self._public_range(sid, scene_id, cell, heights, package)

    def _public_range(self, sid, scene, cell, heights, package):
        """Read cached height-interval results through the public disclosure policy."""
        return self._cached_range(sid, scene, cell, heights, package, public=True)

    def query_unit(self, sid, actor_id, package, *, context):
        """Read unit height exposure, returning explicit missing input when height is
        unavailable.
        """
        with self.h._transaction():
            position = self.h._spatial_reader(sid, actor_id, package, context)
            self.h._visible_target(sid, actor_id, position.cell, package)
            heights = (
                self.unit_height_provider.environment_heights(
                    sid, actor_id, package, position=position
                )
                if self.unit_height_provider is not None
                else None
            )
            if heights is None:
                return UnitEnvironment("missing_input")
            return UnitEnvironment(
                "ready",
                self._public_range(
                    sid,
                    position.scene_id,
                    position.cell,
                    heights,
                    package,
                ),
            )
