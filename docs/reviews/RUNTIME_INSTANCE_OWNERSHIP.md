# Runtime instance ownership — 2026-10-22

Start: `refactor/session-memory-runtime@16c305999e5ac17ef4194a3113eeb60b9ed36902`;
`origin/main@3aaceccf1b1b492f8dd99e3bec16b9379ac6dc77`. The untracked user text
file is excluded. Architecture baseline: `agentic-trpg@5619e7a`; its isolated
documentation branch is `docs/runtime-instance-ownership`. RE and Pages are excluded.

## Pre-change ownership and migration inventory

| Data / current production reader | Before | Intended instance field / maintainer / dependencies |
|---|---|---|
| Object row, effective configuration | WorldFacts / InstanceData; indexed SessionMemory rows and generic parsed cache | Object runtime facts/configuration; original Facts remain the only writers; instance row/config dependencies |
| Direct contents and recursive weight / object view, capacity admission | ContainmentState adjacency and operation weights; generic `total_weight` key sorts every candidate row | Object.container.contents, own weight and subtree total; ContainerService computes on demand from parent-indexed Facts; quantity/config/children dependency closure |
| Scene direct objects | `scene_object_rows` assembles roots and descendants | Scene.objects.direct contains only direct ground objects; effective contributors include carrier/container links separately |
| Trigger and component links / automatic, damage | generic Session-wide `_build_index`, batch dict | Scene.mechanisms.index; object mechanism configuration/links; cross-scene component lookup remains World-owned; changes to membership/config/pose invalidate relevant relationships |
| Parsed map/topology, occupants / Grid, views, RE | GridMaps content pool and generic scoped reads | Scene.spatial.map/occupants; indexed cell lookup remains the local consumer; maps immutable, positions remain actor/object facts |
| Environment / area, public cells, height queries | service DerivedCache and repeated preparation wrappers | Scene.environment inputs/protection/cell/range fields; preserve source/protection versions, provider identity, heights, disclosure and bounded capacity |
| Light sources/results / cell query, GridView, snapshot | generic `sources`/`project`; cell query calculates whole grid | Scene.lighting sources/cell/full projection; source state, position, enclosing transmission and geometry dependencies; public visibility still checked |
| Recovery/current readers | shared pause flag; only recovering connection clears RAM | shared recovery generation; each connection invalidates on its own thread before current reads; old read scopes cannot republish |

All runtime fields are derived representations under the existing SessionMemory,
UnitOfWork/savepoint and durable-COMMIT/publication boundary. Creation and recovery
load persisted instance facts; cold fields are lazy. Rollback discards candidates;
unload/LRU/close release fields. No second Session/transaction executor, Cell system,
rule protocol, periodic-only saving or Package fallback is introduced.

## Implemented owner fields and actual consumers

`runtime_instances.py` defines immutable ObjectRuntime, SceneRuntime and WorldRuntime
owners with typed component groups. Existing SessionMemory stores their field values
under the same entry/byte budget; existing UnitOfWork stages and publishes replacements.
No new transaction, authority, persistent total-weight column or public protocol exists.

- WorldFacts.object / InstanceData.object_definition now resolve Object fields.
  Direct contents belong to Object.container; Scene.objects.direct contains ground
  IDs only, while contributors follow explicit carrier/container relationships.
- `get_object` and `list_scene_objects` read contents and derived weights directly.
  They do not construct ContainmentState. `total_weight` still supports detached
  candidate admission but has no scoped-read key; `runtime_key` is removed.
  Candidate relocation marks original/new ancestors; only an untouched subtree in
  the exact originating UnitOfWork and mutation stage can reuse instance weight.
  Changed ancestors read candidate adjacency, with existing fit/capacity/unknown,
  access/binding/cycle/nesting checks. Weight/config/quantity dependencies and the
  original destroyed-shell semantics are retained; partial quantity transfer remains unsupported.
- Automatic mechanism events use Scene.mechanisms.index after event-kind/phase
  filtering. Object.mechanism holds static trigger/component links, without
  fabricating a current status. Activation/disarm/reset/broken state remains a
  runtime prerequisite. World.mechanisms owns cross-scene component reverse links.
  Its cold scope is configured mechanisms, not all objects or unrelated capabilities.
- GridReader prepared/current maps, object positions, occupants and cell lookup
  are Scene.spatial fields. `local_topology` follows the existing actor-position and
  Schema40 cell indices for the requested cells and diagonal neighbors. It no
  longer uses a whole-scene occupant list. Mechanical maps do not imply fine
  spatial grants. Object-only geometry consumers do not acquire actor-position dependencies.
- Environment public cell/area/unit/height and internal projections use
  Scene.environment facts/inputs/protection/cells/ranges. The existing registered
  source/protection/provider version protocol remains authoritative; incidental
  decoder reads do not make lock metadata a rain-protection dependency. Unregistered
  providers still opt out. Area admission checks only the target instance and map
  bounds, without composing environment or loading all Scene configurations.
- Light sources, geometry, bounded cell results and explicit full-map projection
  belong to Scene.lighting. Public single-cell queries compose just that cell.
  Grid base topology, noncombat RE Snapshot and noncombat staged evidence share
  the current projection. Explicit combat/fixed-map inputs keep their supplied
  geometry and are not aliased to the current scene map. Visibility, sources and
  uncertainty still follow the original public/RE rules.

  Targeted object/door/area visibility uses only the requested cells' lighting.
  `ReadOnlyTopology.with_lighting` shares immutable parsed walls/blockers and adds
  a detached read-only lighting overlay; it does not parse/serialize the map again
  for each object. The pinned RE `can_see` algorithm is unchanged. Step/transfer
  bounds checks do not prepare lighting they do not consume. Complete scene views
  and mechanical/evidence inputs retain their explicit full-map scope.

### Relationship and validity maintenance

Simple RAM queries record predicate membership plus the columns actually selected;
unselected status changes do not invalidate identity-only association reads.
`SessionMemory.changed` advances affected projection dependencies and scene capability
association versions using the changed position and necessary parent/carrier chain.
Those chain reads use the same RAM/candidate interface, rather than bypassing loaded
facts with raw SQL. New/old scene associations, actor scene changes, capability
insert/delete and controlled REPLACE are covered. Savepoint restoration restores
the same sparse candidates and versions.

Cold capability discovery follows indexed ground roots/carriers/parent links and
filters identities before reading configuration or capability state. Its resulting
field depends on a maintained Scene association version and the selected instances,
not one validity probe for every unrelated object. Empty mechanism results remain
valid until a relevant relationship/configuration change. This is lazy association
loading, not a claim that cold directory construction is constant-cost.

Fields are immutable replacements; returned mutable values are detached. Nested
computations preserve other fields filled in the same scope. They participate in
the existing memory_pending publication and shallow savepoint snapshots safely.
Source/protection/height/provider identities still distinguish environment results;
clear/release epochs prevent pending old values from being considered current.
Per-field bounded lists and the original 2,048-entry / 32 MiB retained-payload budget
permit cold reload and eviction. This is not a complete Python heap upper bound.

### Cross-connection recovery

The shared database coordinator records Session recovery generations. Pause and
reconciliation advance the relevant generation. Each connection synchronizes it
on its own thread before opening a current reading scope, releasing that Session's
old fields/maps and obsolete TimeDriver. A read scope captures its generation and
cannot republish after a later recovery, even when SQLite data_version is unchanged.
Read-only recovery does not erase unrelated Session fields. No connection is used
from another thread; stale drivers are detached and require explicit reattachment.

Existing pre-commit rollback, unknown commit, committed-publication-pending,
receipt replay and shared pause semantics remain. Failed recovery leaves all
contexts paused. Fixed historical inputs remain valid historical values. SQLite
and RAM are still not a cross-medium ACID transaction.

## Deterministic public-path measurements

Executed `uv run --locked --no-sync python tests/_session_memory_benchmark.py
--output .test-tmp/runtime-instance-delivery-measurements.json`, exit 0. Both groups
warm two Put/Take cycles, measure 10 public object reads and 20 public Put/Take
commands. Growth adds 150 unrelated objects and 150 remote actors; objects occupy
both existing scenes. Separate scoped tests add 20 scenes and exercise cold local
admission and loaded-cell reuse. These are synthetic local measurements, not a
multiplayer load/SLO claim.

| Independent count | Growth 0 | Growth 150 |
|---|---:|---:|
| 10 hot reads: SQL SELECT / fetched rows | 0 / 0 | 0 / 0 |
| Hot JSON / model parses / fixed Snapshot builds | 0 / 0 / 0 | 0 / 0 / 0 |
| Hot instance field computations / hits | 0 / 360 | 0 / 360 |
| Hot RAM row accesses | 130 | 130 |
| 20 commands: SQL SELECT / fetched rows | 180 / 80 | 180 / 80 |
| SQL returned encoded bytes | 69,350 | 69,350 |
| Command RAM row accesses / published row updates | 829 / 160 | 829 / 160 |
| Command instance field computations / hits | 117 / 890 | 117 / 890 |
| Four commands: SQLite VM instructions | 8,356 | 8,356 |
| Retained scalar payload bytes | 673,359 | 836,074 |
| tracemalloc added retained / peak bytes | 1,117,579 / 1,232,898 | 1,117,619 / 1,232,556 |

Hot scopes still execute 10 BEGIN and 30 PRAGMA metadata fences plus permission /
disclosure checks. Commands retain 160 json.loads calls for new event persistence,
event reads and perception proof (40/80/40); zero model/content-cache parses does
not mean all JSON work disappeared. `parsed_builds=138` is generic wrapper work,
not a JSON counter. Necessary fit/aggregate checks, persistence and reliable keys
remain; there is no zero-SQL write claim.

The existing 100-content public cases (direct and nested) now construct **zero**
ContainmentState candidates for get/list, while preserving weights 100 / 102.
The full-candidate key constructor no longer exists. New public field tests forbid
the old `_rows` / `_weight` path during object reads. Single-cell light composition
observes exactly the one requested cell and then reuses it. Empty mechanism index
builds once across two real StepActor commands. Timing was collected under
instrumentation/concurrent validation and is not used as scale evidence.

The supplied historical 10,302 figure describes records visited while building
candidate cache keys, not SQL statements; it is not relabeled or claimed as a
fresh baseline measurement here.

## Acceptance and remaining boundaries

Final executed collection, disjoint JUnit accounting and checks are recorded in
the delivery entry below. Early runs exposed old tests that observed removed
candidate/project/cache internals; those assertions were migrated to production
field bodies and actual owner values, retaining behavior, authorization and
old-reader/version checks. The Environment input counter improves from two source
preparations for two cells to one shared input preparation. Test fixtures that
alter instance config perform explicit structural acceptance before public reads;
this does not create a new live configuration-edit API.

The restricted environment stalled before real RE execution in Windows
asyncio `_fallback_socketpair` / `socket.accept`. Those runs were stopped and the
same locked environment was run outside the sandbox. An initial measurement found
40 unnecessary position SELECTs in change callbacks (220 SELECT / 120 rows); those
reads were moved to RAM before the final 180 / 80 measurement. Interrupted full
runs and intermediate failures are not counted as final acceptance.

A subsequent complete-call-chain check found the old single-object visibility
consumer still entering a full light projection. The target-cell overlay above
closes that path; a public get_object test forbids whole-map projection and checks
that the old base geometry/lighting stays immutable. Final checks below include
this final consumer migration; earlier runs are intermediate evidence.

Cold association discovery and requested complete geometry/projections retain
their declared directory/mechanical scope; this work does not invent a constant-cost
full-map algorithm. Provider production support is unchanged. There is no separate
unload_scene API; Session unload/LRU/close govern retained fields, and unloading
does not destroy persistent actors/items. Full Effects/Activity, arbitrary Cell
instances, multiplayer protocol, partial item splitting and active-combat crash
continuation remain outside this change. Schema40, DTO/RE pin and rules are unchanged.

## Final verified delivery

Named fields/consumers/recovery fixes: Implementation Status **[IMPLEMENTED]**;
Acceptance Status **[VERIFIED]** for the scope above. This does not close the
larger unsupported systems listed above. The final runtime, tests and examples
were held fixed throughout this complete acceptance (SHA256 comparison: zero changes).

| Executed check | Exit / evidence |
|---|---|
| `uv run --locked --no-sync ruff check src tests examples` | 0 |
| `uv run --locked --no-sync ruff format --check src tests examples` | 0; 214 files |
| `uv run --locked --no-sync pytest --collect-only -q` | 0; 2,059 tests in 92 files |
| Complete file partition 0 | 0; 633 passed; JUnit 801.461 s |
| Complete file partition 1 | 0; 738 passed; JUnit 539.861 s |
| Complete file partition 2 | 0; 688 passed; JUnit 467.461 s |
| JUnit / collection / SHA256 audit | 0; 2,059 unique tests; missing / unexpected / duplicate / failure / error / skip all zero |
| Final public-path measurement | 0; counts in the table above |
| Both repositories' changed Markdown links / anchors / fences | 0; 18 files / 172 local links |
| Staged and unstaged `git diff --check` | 0 |
| Dependency/lock/SQL migration diff | Empty |

The three complete partitions are disjoint sorted test files at indices `part,
part+3, ...`; this is local acceptance grouping, not a new CI tier. Each used
`uv run --locked --no-sync pytest -q @taskSubset`, independent
`--basetemp=.test-tmp/instance-complete-N`, `-o faulthandler_timeout=60` and
`--junitxml=.test-tmp/instance-complete-N.xml`. The existing locked `.venv` was
reused, with repository UV_CACHE_DIR, PYTHONUTF8 and cleared process PYTHONPATH
for historical subprocess isolation. Historical fixtures retain their own old
locked bindings; no fixture or production assertion was weakened to accept a new pin.

Final raw outputs are `instance-complete-N.log/xml`, `instance-complete-collection.txt`,
`instance-complete-hashes.json`, `instance-complete-audit.json`,
`runtime-instance-delivery-measurements.json` and `instance-doc-check.json` under
this checkout's ignored `.test-tmp`. Only the same two existing Lark deprecation
warnings occur in each partition. Focused runs are not added again to 2,059.
These are local checks, not GitHub Actions results.

Delivery keeps SM on its original branch and the docs on the new isolated branch;
only named task files are committed. The user text and original docs requirements
remain unstaged. The docs worktree remains under
`.worktrees/runtime-instance-ownership/agentic-trpg` for review. Final commit IDs
and normal push verification are supplied in the delivery response; neither main
branch is merged or pushed by this task.
