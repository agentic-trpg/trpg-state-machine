"""Scene environment business and read-only derived queries over shared WorldFacts."""

from dataclasses import asdict, dataclass, replace

from .derived_cache import DerivedCache
from .environment_commands import (
    AddEnvironmentSource,
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
    validate_sources,
)
from .environment_versions import EnvironmentVersions
from .errors import StateMachineError
from .grid import cell_in_bounds
from .object_protection import PRECIPITATION_KINDS


@dataclass(frozen=True, slots=True)
class ConditionContribution:
    source_ref: str
    value: Temperature | Wind | Precipitation | Obscurant
    exposure: str
    observable: bool


@dataclass(frozen=True, slots=True)
class ResolvedCondition:
    status: str
    contributions: tuple[ConditionContribution, ...] = ()


@dataclass(frozen=True, slots=True)
class ProtectionBand:
    source_ref: str
    heights: HeightInterval


@dataclass(frozen=True, slots=True)
class CellProtection:
    rain_status: str
    rain_sources: tuple[str, ...]
    wind_status: str
    wind_bands: tuple[ProtectionBand, ...]
    precipitation: tuple[tuple[str, str, tuple[str, ...]], ...] = ()

    def for_precipitation(self, kind):
        if kind == "rain":
            return self.rain_status, self.rain_sources
        return next(
            ((status, refs) for k, status, refs in self.precipitation if k == kind), ("unknown", ())
        )


@dataclass(frozen=True, slots=True)
class CellEnvironment:
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
    heights: HeightInterval
    environment: CellEnvironment


@dataclass(frozen=True, slots=True)
class UnitEnvironment:
    status: str
    segments: tuple[EnvironmentHeightSegment, ...] = ()


def _value_status(value):
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
    def __init__(
        self,
        handler,
        *,
        command_submitter=None,
        effects_provider: EnvironmentEffectsProvider | None = None,
        unit_height_provider: UnitHeightProvider | None = None,
    ):
        self.h = handler
        self.command_submitter = command_submitter
        self.effects_provider = effects_provider
        self.unit_height_provider = unit_height_provider
        self._projections = DerivedCache(32)
        self._cells = DerivedCache(512)
        self.versions = EnvironmentVersions(handler.store)

    def clear_snapshots(self):
        """Disposable derived state only; also cancel publication of pending old entries."""
        self._projections.clear()
        self._cells.clear()

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
        if "inputs" not in cache:
            prepared = self.h.objects.spatial.prepare_scene_protection(sid, scene, package)
            cache["inputs"] = replace(
                prepared, objects=tuple(p for p in prepared.objects if not p.explicitly_empty())
            )
        return cache["inputs"]

    def _environment_inputs(self, sid, scene, package, cache, public):
        if "environment" not in cache:
            cache["environment"] = self._inputs(sid, scene, package)
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
            raw = self.h.grid.reader.scene_grid(sid, scene_id, package)
            if raw["width"] * raw["height"] > MAX_CELLS:
                invalid("Whole Scene query exceeds 256 cells; choose a bounded area")
            return tuple(f"{x},{y}" for x in range(raw["width"]) for y in range(raw["height"]))
        return ()

    def _validate_facts(self, sid, scene, facts, package):
        self._area(sid, scene, EnvironmentArea(), package)
        for source in facts.sources:
            self._area(sid, scene, source.area, package)
        if facts.protection.rain_cells:
            self._area(sid, scene, EnvironmentArea(facts.protection.rain_cells), package)
        for protection in facts.protection.precipitation or ():
            self._area(sid, scene, EnvironmentArea(protection.cells), package)
        for wind in facts.protection.wind or ():
            self._area(sid, scene, EnvironmentArea((wind.cell,)), package)

    def operate(self, row, op, package):
        """Trusted WorldHandler participant. Plan the entire batch BEFORE touching facts."""
        self.h.store.require_write()
        validate_environment_operation(op)
        sid = row["session_id"]
        operations = op.changes if type(op) is BatchEnvironment else (op,)
        originals, planned, changes = {}, {}, []
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
                    sources[source_id] = change.source
                elif type(change) is ReplaceEnvironmentSource:
                    if before is None:
                        raise StateMachineError("environment_source_not_found", "Source is absent")
                    sources[source_id] = change.source
                else:
                    sources.pop(source_id, None)
                after = sources.get(source_id)
                if before == after:
                    continue
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

    def read_facts(self, sid, scene_id, package, *, context):
        """GM import/audit API, never a player source catalogue."""
        with self.h._transaction():
            self.h._session(sid, package, context)
            if not context.can_manage_environment:
                raise StateMachineError("unauthorized", "Environment management grant required")
            self._area(sid, scene_id, EnvironmentArea(), package)
            return self.h.facts.environment(sid, scene_id)

    def _inputs(self, sid, scene, package):
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
    def _conditions(facts, effects, cell, height):
        candidates = {k: [] for k in CONDITION_TYPES}
        if facts.base is not None:
            for k in candidates:
                value = getattr(facts.base, k)
                if value is not None:
                    candidates[k].append(
                        (-1001, ConditionContribution("base", value, "outdoor", True))
                    )
        for prefix, sources in (("fact:", facts.sources), ("effect:", effects)):
            for source in sources:
                area = source.area
                if area.cells and cell not in area.cells:
                    continue
                if (
                    area.heights
                    and height is not None
                    and not (area.heights.lower_ft <= height < area.heights.upper_ft)
                ):
                    continue
                for k, cls in CONDITION_TYPES.items():
                    value = getattr(source.conditions, k)
                    if value is None:
                        continue
                    if area.heights and height is None:
                        value = cls()  # Applicable vertical scope is unknown, never guessed.
                    candidates[k].append(
                        (
                            source.priority,
                            ConditionContribution(
                                prefix + source.source_id, value, source.exposure, source.observable
                            ),
                        )
                    )
        return {k: _resolve(v, layers=k == "obscurant") for k, v in candidates.items()}

    def _protection(self, sid, scene, cell, package, facts, direction, cache):
        # A placeholder cardinal direction selects ONLY rain when actual wind is unknown.
        key = direction or "north"
        if "identity" not in cache:
            cache.update(self._query_cache(sid, scene, package))
        if key not in cache:
            cache[key] = self._projections.resolve(
                cache["scope"],
                (cache["identity"], (cache["versions"][1],), key),
                lambda: self.h.objects.spatial.scene_protection_contributions(
                    sid,
                    scene,
                    package,
                    wind_from=key,
                    prepared=self._prepared_protection(sid, scene, package, cache),
                ),
                validator=cache["protection_valid"],
            )
        oss = cache[key]
        bands = [
            ProtectionBand("static", w.heights)
            for w in facts.protection.wind or ()
            if w.cell == cell and w.wind_from == direction
        ]
        if direction is not None:
            bands += [
                ProtectionBand(
                    "object:" + o.object_id, HeightInterval(c.lower_height_ft, c.upper_height_ft)
                )
                for o in oss.objects
                for c in o.wind_cells
                if c.cell == cell
            ]
        if len(bands) > 4096:
            invalid("Protection query exceeds 4096 source bands")

        def completeness(static, statuses):
            if static is None or any(s != "none" and s != "ready" for s in statuses):
                return "unknown"
            return "none"

        precipitation = []
        for kind in PRECIPITATION_KINDS:
            refs = (
                ["static"]
                if (
                    kind == "rain"
                    and cell in (facts.protection.rain_cells or ())
                    or any(
                        cell in p.cells and kind in p.kinds
                        for p in facts.protection.precipitation or ()
                    )
                )
                else []
            )
            refs += [
                "object:" + o.object_id
                for o in oss.objects
                if (
                    any(c.cell == cell for c in o.rain_cells)
                    if kind == "rain"
                    else any(c.cell == cell and c.kind == kind for c in o.precipitation_cells)
                )
            ]
            static = facts.protection.precipitation
            if static is None:
                static = facts.protection.rain_cells
            status = (
                "protected"
                if refs
                else completeness(static, (o.precipitation_status(kind) for o in oss.objects))
            )
            precipitation.append((kind, status, tuple(sorted(set(refs)))))
        _, rain_status, rain = precipitation[0]
        wind_status = (
            completeness(facts.protection.wind, (o.wind_status for o in oss.objects))
            if direction is not None
            else "missing_input"
        )
        return CellProtection(rain_status, rain, wind_status, tuple(bands), tuple(precipitation))

    @staticmethod
    def _exposure(condition, protection_status, covered, *, wind=False, protection=None):
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

    def _cell(
        self, sid, scene, cell, package, height, inputs, cache, *, public=False, validated=False
    ):
        if "identity" not in cache:
            cache.update(self._query_cache(sid, scene, package))

        def compute():
            if not validated:
                self._area(sid, scene, EnvironmentArea((cell,)), package)
            current, hidden = self._environment_inputs(sid, scene, package, cache, public)
            result = self._compose_cell(sid, scene, cell, package, height, current, cache)
            return self._public(result, hidden) if public else result

        return self._cells.resolve(
            cache["scope"],
            (cache["identity"], cache["versions"], ("cell", public, cell, height)),
            compute,
            validator=cache["valid"],
        )

    def _compose_cell(self, sid, scene, cell, package, height, inputs, cache):
        facts, effects, effects_status = inputs
        conditions = self._conditions(facts, effects, cell, height)
        wind = conditions["wind"]
        direction = wind.contributions[0].value.wind_from if wind.status == "ready" else None
        protection = self._protection(sid, scene, cell, package, facts, direction, cache)
        covered = height is not None and any(
            b.heights.lower_ft <= height < b.heights.upper_ft for b in protection.wind_bands
        )
        # Height applies to wind/ambient conditions. Rain exposure is a separate ground model.
        ground_conditions = self._conditions(facts, effects, cell, None)["precipitation"]
        return CellEnvironment(
            scene,
            cell,
            height,
            **conditions,
            ground_precipitation=self._exposure(
                ground_conditions,
                protection.rain_status,
                bool(protection.rain_sources),
                protection=protection,
            ),
            wind_exposure=self._exposure(
                wind,
                protection.wind_status if height is not None else "missing_input",
                covered,
                wind=True,
            ),
            protection=protection,
            effects_status=effects_status,
        )

    def project_cell(self, sid, scene_id, cell, package, *, height_ft=None):
        """INTERNAL trusted composition: caller checks binding/grants and owns read scope."""
        if height_ft is not None:
            finite(height_ft)
        EnvironmentArea((cell,))
        with self.h.grid.reader.read_scope():
            return self._cell(sid, scene_id, cell, package, height_ft, None, {})

    def _range(self, sid, scene, cell, package, heights, inputs, cache):
        if type(heights) is not HeightInterval:
            invalid("Expected scene-datum height interval")
        self._area(sid, scene, EnvironmentArea((cell,)), package)
        facts, effects, _ = inputs
        points = {heights.lower_ft, heights.upper_ft}
        for source in (*facts.sources, *effects):
            if source.area.heights and (not source.area.cells or cell in source.area.cells):
                points.update((source.area.heights.lower_ft, source.area.heights.upper_ft))
        # Wind may change with height. Collect all four directions, retaining every gap.
        for direction in ("north", "east", "south", "west"):
            projection = self._protection(sid, scene, cell, package, facts, direction, cache)
            for band in projection.wind_bands:
                points.update((band.heights.lower_ft, band.heights.upper_ft))
        points = sorted(p for p in points if heights.lower_ft <= p <= heights.upper_ft)
        if len(points) > 512:
            invalid("Height query exceeds 512 boundaries")
        return tuple(
            EnvironmentHeightSegment(
                HeightInterval(a, b),
                self._compose_cell(sid, scene, cell, package, a, inputs, cache),
            )
            for a, b in zip(points, points[1:], strict=False)
        )

    def _cached_range(self, sid, scene, cell, heights, package, *, public=False):
        if type(heights) is not HeightInterval:
            invalid("Expected scene-datum height interval")
        EnvironmentArea((cell,))
        cache = self._query_cache(sid, scene, package)

        def compute():
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
        def redact(condition):
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
        if height_ft is not None:
            finite(height_ft)
        if type(area) is not EnvironmentArea or area.heights is not None:
            invalid("Area point query takes separate explicit height_ft")
        with self.h._transaction():
            position = self.h._spatial_reader(sid, actor_id, package, context)
            if position.scene_id != scene_id:
                raise StateMachineError("unauthorized", "Only Actor's current Scene is authorized")
            visibility = self.h._prepare_visible_targets(sid, actor_id, package)
            cells = area.cells
            if not cells:
                width, height = visibility[2]
                if width * height > MAX_CELLS:
                    invalid("Whole Scene query exceeds 256 cells; choose a bounded area")
                cells = tuple(f"{x},{y}" for x in range(width) for y in range(height))
            for cell in cells:
                if not cell_in_bounds(visibility[1], cell):
                    raise StateMachineError("invalid_cell", "Environment area exceeds Scene bounds")
                self.h._visible_target(sid, actor_id, cell, package, prepared=visibility)
            cache = self._query_cache(sid, scene_id, package)
            return tuple(
                self._cell(
                    sid,
                    scene_id,
                    cell,
                    package,
                    height_ft,
                    None,
                    cache,
                    public=True,
                    validated=True,
                )
                for cell in cells
            )

    def query_cell(self, sid, actor_id, scene_id, cell, package, *, context, height_ft=None):
        return self.query_area(
            sid,
            actor_id,
            scene_id,
            EnvironmentArea((cell,)),
            package,
            context=context,
            height_ft=height_ft,
        )[0]

    def query_height_range(self, sid, actor_id, scene_id, cell, heights, package, *, context):
        with self.h._transaction():
            position = self.h._spatial_reader(sid, actor_id, package, context)
            if position.scene_id != scene_id:
                raise StateMachineError("unauthorized", "Only Actor's current Scene is authorized")
            self.h._visible_target(sid, actor_id, cell, package)
            return self._public_range(sid, scene_id, cell, heights, package)

    def _public_range(self, sid, scene, cell, heights, package):
        return self._cached_range(sid, scene, cell, heights, package, public=True)

    def query_unit(self, sid, actor_id, package, *, context):
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
