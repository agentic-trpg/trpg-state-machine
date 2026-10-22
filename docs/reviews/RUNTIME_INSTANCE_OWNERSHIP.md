# Runtime instance ownership — 2026-10-22

Latest follow-up: [managed identity/version cache inputs](#2026-10-22-follow-up-managed-cache-inputs).
Earlier acceptance counts remain dated evidence for their original source.

The follow-up [mutation-maintained validity acceptance](#2026-10-22-follow-up-mutation-maintained-validity)
below supersedes the original warm-read cost claims only within its measured scope.
All original counts and acceptance dates below retain their original meaning.

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
## 2026-10-22 follow-up: mutation-maintained validity

Start/review baseline: `refactor/session-memory-runtime@c64f9d7975cad55e536e0e4ea7a056fed79bd384`.
Paired docs baseline: `docs/runtime-instance-ownership@80bd39c551dd62cb53bfd2d4a18f4c4c3a00182a`.
Both existing branches matched their remote refs after fetch. Original untracked
user text and nested worktrees are preserved. No new branch, main integration,
RE change, Pages edit, schema/DTO/dependency/rule change is part of this follow-up.

### Actual maintenance and isolation

- **Weight:** the three Object.container scalar fields use one object weight
  version. `SessionMemory.changed` invalidates the changed item's fields and the
  old/new ancestor chains for parent membership, item JSON (including quantity),
  creation/removal and persisted object configuration changes. Valid reads check
  that field version once; computation collects its leaves privately, so enclosing
  get/list, capacity and parsed caches inherit the field version rather than all
  descendants. Missing/stale fields compute from their actual candidate stage and
  retain that stage's stamp. Unknown remains `None`. Destruction still retains the
  supported shell/contents semantics; actual relocations/configuration changes,
  rather than the word "destroyed", determine weight effects.
- **Mechanisms:** Scene.mechanisms.index uses a maintained scene index version.
  Membership/effective-scene and mechanism configuration changes invalidate it;
  arming/status/uses and eligibility are still checked on the selected mechanism.
  `TriggerIndex` wraps a private read-only mapping of tuple buckets and frozen
  trigger records. `automatic` gets the event/target bucket directly. Sharing an
  index never copies its records or exposes writable nested state. A relevant
  change may rebuild the actual scene mechanism index lazily; ordinary hits do
  not validate every configuration. The World component reverse index keeps its
  existing separate dependency/isolation contract.
  Region buckets are keyed by destination cell, and object-operation buckets by
  the concrete event type and object ID. An event of the same category at another
  target therefore selects zero records too; matching entry order/conditions stay
  unchanged. Region preparation/storage costs the configured region-cell links.
- **Capability associations:** Scene.objects.capabilities holds the two immutable
  light/mechanism ID tuples. They derive solely from objects' authoritative
  parent/carrier/scene facts, actors' scenes and capability membership tables.
  Cold construction follows existing root/carrier/parent indexes. Normal cross-scene
  movement follows only the moving subtree (or an actor's carried subtrees), checks
  their capability member buckets and patches loaded old/new scene associations.
  No scene scan decides whether a moved bag has relevant descendants. Same-scene
  transfer does not change membership. Selected source/ancestor/configuration,
  enabled/mode/HP and carrier dependencies still invalidate light contributions
  when their position, shading or accessibility actually changes. Capability
  insertion/removal patches only its effective scene. No independently writable
  membership truth or new Session/transaction manager is introduced.
- **Indirect light costs:** lighting.candidates separately holds ground-object
  uncertainty. Its version changes with ground pose/state, object configuration,
  durability, scene acceptance lifecycle and controlled layout configuration.
  Nonground-to-nonground empty-bag transfer does not change it. This removes the
  negative spatial/configuration dependency expansion through cached light
  geometry, projection and visible_cells. Door, map, source and shading inputs
  keep their original separate checks; no geometry/rule behavior is removed.
- **Retention cost:** each immutable FieldValue computes its conservative scalar
  payload charge when produced/replaced. Owner budget accounting sums those
  charges without formatting retained values again. DependencyGroups shares the
  fields' immutable dependency tuples without flattening every leaf on each
  owner replacement; trimming still retains the actual dependencies. The existing
  entry/byte limits remain, with an assertion that the trigger payload is charged,
  rather than hiding it behind a short repr. This removes a separately reproduced
  10/100-record budget repr traversal even after index deepcopy was eliminated.

Facts, parent buckets, association patches and invalidation versions all use the
existing sparse UnitOfWork, savepoint restoration and post-COMMIT publication.
Object DELETE capture runs before removal so necessary position/contribution facts
are available; failure still rolls back the same candidate. Published owners are
immutable replacements. Missing/evicted fields stay cold; patching never stamps
an already-invalid association as valid. Controlled REPLACE conservatively
invalidates affected loaded protocols because SQLite may omit victim DELETE
callbacks; this is a maintenance compatibility path, not a normal command scan.
Unload, restart and all-context recovery discard/rebuild these derived fields
from persisted instances. The prior shared recovery generations, paused Sessions,
old-read publication fence and driver invalidation remain intact.

### Matched deterministic measurements

`tests/test_runtime_field_maintenance.py` counts real `memory.version` calls,
RAM query calls/visited rows, SQL SELECTs/fetched rows, resolver compute bodies,
association discoveries and copied trigger records independently. All are
production paths; wrappers observe work without replacing results. Baseline source
was loaded from a read-only `git archive c64f9d7 src` in the same locked environment.
The eight new performance cases fail on that baseline as expected; the original
baseline reproduction without the new assertions passed all six cases.

RAM calls mean the SessionMemory.select interface (including cold requests that
also execute SQL); visited RAM rows and fetched SQL rows are separate counters.
Copied records mean scene trigger-index records, not necessary complete-map or
event-output copies. Trigger __repr__ calls separately detect record traversal
during retention accounting.

| Scope / count | Before small / large | After small / large |
|---|---:|---:|
| One warm weight, 10 / 100 contents: dependency probes | 31 / 301 | 1 / 1 |
| Same weight: SQL SELECTs, fetched rows, RAM queries, computations | all 0 / 0 | all 0 / 0 |
| Real nonmatching StepActor, 10 / 100 mechanisms: copied trigger records | 10 / 100 | 0 / 0 |
| Same StepActor: trigger records formatted for budget accounting | 10 / 100 | 0 / 0 |
| Same-category region event outside all configured cells: selected candidates | 10 / 100 | 0 / 0 |
| Same StepActor: all dependency probes | 121 / 211 | 111 / 111 |
| Same StepActor: scene-index probes / index rebuilds | per-member dependencies / 0 | 1 / 0 in both |
| Same StepActor: SQL SELECTs / fetched rows | 26 / 5 in both | 26 / 5 in both |
| Same StepActor: RAM calls / rows | 78 / 32 in both | 78 / 32 in both |
| Four empty-bag Put/Take commands, growth 0 / 100: association discoveries | 8 / 8 | 0 / 0 |
| Same commands: RAM calls | 350 / 750 | 284 / 284 |
| Same commands: RAM rows | 202 / 202 | 180 / 180 |
| Same commands: all dependency probes | 1,902 / 6,302 | 972 / 972 |
| Same commands: scene mechanism index / light source rebuilds | 4 / 4 in both | 0 / 0 in both |
| Same commands: SQL SELECTs / fetched rows | 36 / 16 in both | 36 / 16 in both |
| Same commands: all instance computations | 28 / 28 | 16 / 16 |

Growth adds 100 unrelated objects (50 in this scene) **and 100 remote actors**.
The light, target operation and actual target contents remain fixed. Both groups
warm two Put/Take cycles. Subsequent commands use receipt versions; a separately
requested complete SessionView is outside this measured ordinary-command scope.
StepActor fixtures use one legal nonmatching trigger on each of 10/100 mechanisms,
testing both a different event type and a same-category different destination cell,
respecting the sixteen-trigger-per-mechanism limit. Weight repeats also prove
three reads perform exactly three probes and no computation; warmed public get/list
checks prevent upper-layer dependency expansion while allowing complete contents
output. The 16 remaining command computations are necessary facts/configuration,
direct contents and candidate weight work; there is no zero-work/zero-SQL write
claim. SessionMemory content JSON/model decoding and its fixed Snapshot wrapper
construction counters are zero in these hot measurements. New event JSON work is
retained and is not counted as zero by this table. Full event evidence/persistence
and required admission remain.

The historical 15/115 RAM-query discovery reproduction, 31/301 dependency counts,
10/100 copied records and earlier 2,059-test/SQL measurements keep their original
scope/time. They are not retroactively relabeled as this acceptance.

### Validation and remaining scope

Final executed tests and checks are recorded at delivery below. New regressions
cover nested quantity/mass changes, unknown weights, multiple mutation stages,
candidate relocation, savepoint/outer rollback, unload/restart; nested light and
mechanism carrier scene changes, opaque-container shading, capability membership
remove/add and fixed old indices/inputs. Existing cross-connection publication
failure tests now also assert old/current weights. Existing public capacity,
matching trigger qualification/order/uses, destruction, authorization/disclosure,
idempotency, RNG, commit failure and recovery tests remain in acceptance.
Final candidate tests also cover positional/keyword capability-query identity and
retained payload charging. The earlier full 2,067-test run preceded the last
retention-cost fix and is intermediate evidence; final acceptance is rerun against
the frozen final sources, not inferred from that run.

Cold association preparation and invalidated whole-map geometry have their actual
directory/map cost. Cross-scene maintenance traverses the moved subtree, not an
unrelated scene; association tuple replacement and changed mechanism index rebuilds
cost their actual capability members/triggers. Ground geometry invalidation is
conservative for ground-object configuration/state and scene-instance metadata changes. Full lists, complete
mechanical/evidence maps and necessary aggregate admission remain output/dependency
sized. No public partial-stack transfer, general live capability editor or standalone
scene unload API is added. These are bounded local counters, not multiplayer/SLO
evidence or a complete Python heap bound.

### Final follow-up acceptance

Final fixed sources/tests/examples pass **2,069 unique tests in 93 files**:
three disjoint sorted-file groups of **650 / 725 / 694**, each exit 0. Collection
versus JUnit union has zero missing/unexpected/duplicate/failed/errored/skipped
nodes. All **215 Python source/test/example hashes** are unchanged across this
final run. Historical migration cases are included with no environment-blocked or
skipped cases. The separately executed final mechanism/field/recovery slice passes
60 tests (exit 0); it is not added again to the full-suite total. Earlier runs,
including the superseded interrupted run, are not final acceptance evidence.

Executed with the existing locked environment and repo-local basetemps:
`uv run --locked --no-sync pytest -q` over each sorted test-file partition with
`-o faulthandler_timeout=60` and separate JUnit outputs. The eight old-source
performance cases intentionally fail the new assertions, while their final
production-path counterparts pass. Reproduction and counter definitions live in
the [production-path regressions](../../tests/test_runtime_field_maintenance.py).
SQL, RAM interface/rows, version probes, copied/formatted trigger records,
candidate selection, discovery/rebuild and compute-body counters are separate.

`uv run --locked --no-sync ruff check src tests examples` and
`uv run --locked --no-sync ruff format --check src tests examples` both exit 0
(215 files). Both repos' modified Markdown links/anchors/fences (12 files / 148
local links) and staged/unstaged `git diff --check` pass. Only
the existing two Lark deprecation warnings occur per pytest group. These are local
checks; GitHub Actions and sustained multiplayer load were not executed. Delivery
uses the two original branches and separate commits/pushes; no merge or force push
is authorized.

## 2026-10-22 follow-up: managed cache inputs

Start: SM `refactor/session-memory-runtime@e776fab1ede6ec8fc8ca5a46cb34410a85031de1`;
Doc `a593a9d91b1268bba26805b48b3d2cf4b5f13942`. Fetch/ls-remote confirmed both
reviewed heads. The Doc predecessor is not an ancestor of remote main `5619e7a`;
new branch `docs/runtime-versioned-cache-keys` therefore starts from its latest
head in a new worktree. SM main remains `3aacecc`; existing worktrees, untracked
text and primary Doc files are preserved. RE, Pages, merges and main pushes are
excluded. No Schema, lockfile, rule binding or public DTO change.

### Implemented consumers and validity

| Owner / actual field | Entry and identity | Source / maintenance |
|---|---|---|
| Scene.spatial.record, map, mechanical_map | GridMaps.scene; GridReader.prepared_scene/current_grid; Store + Session + Scene | Existing SessionMemory projection versions for mechanical_scenes state_json/spatial_enabled; actual row changes invalidate, actor/object motion does not |
| Scene.spatial plus local blocker overlay | GridService.local_topology/base_topology/exploration_topology | PreparedGrid shares ReadOnlyTopology; Schema40 cell and Actor position indices, relevant door states and endpoint conditions supply dynamic blockers |
| Scene.lighting.sources/geometry/cells/projection/topology | query_illumination/current_cell/current_project/current_topology | Existing actual source, mode, ancestor transmission, map, ground geometry and door dependencies; no full-map content key for managed input |
| CharacterRuntime.inventory | CharacterFacts.inventory_model, public read_carried_items; Session + actor | Maintained inventory stamp: affected owner changes, aggregate fallback, actor membership and controlled layout version; model schema remains InventoryState |
| CharacterFacts component/selection cache | rule_components, _inventory_selection; Session + explicit owner and item-ID sets | Existing component row/relationship dependency capture; indexed ComponentInputs selects actual closure; no inventory structure-to-JSON-to-model-cache round trip |
| Scene.spatial.sensory | GridMaps.mechanical_grid, EventCapture._perception_geometry; Session + Scene + MechanicalGrid type | Complete MechanicalGrid validation of original prepared data on cache miss/map change; GridScene defaults do not fill missing sensory fields |
| Event stage result | EventCapture stage evidence, signals and persisted evidence JSON | Detached full stage output from that candidate/read range; later map/model changes cannot modify old evidence |

`local_topology` shares the base blocked set through an immutable membership
overlay. Creating it visits only supplied dynamic cells; it never thaws, unions,
sorts or re-encodes the full base map. RE algorithms still decide walls, corners,
diagonals, bounds and cover. Existing exclusion, shared-door and multi-cell
semantics remain. Full exploration/path consumers still read their required
dynamic scope. Public Light retains current authorization, LOS and source
disclosure; its visibility topology comes directly from the same prepared map.
Full Light projection shares base topology and produces a detached output map.
Event capture overlays projected lighting on the already validated stage topology.
Current combat-position consumers also pass PreparedGrid; initial/fixed foundation
maps keep their supplied content identity.

Inventory mutation capture follows old/new container ancestry to actual carriers.
Objects/quantity/unit metadata, actor/core/mechanical facts, object configuration,
durability, bars and locks invalidate affected owners. `sync_inventory_change`
already knows the old/new owners: its synchronous `inventory_projection_write`
capture hint advances only their stamps for the validated inventory patch. This
hint is not a writable fact, another directory or transaction. Other aggregate
SQL/import/schema/resource/casting/effect changes use a conservative shared
fallback stamp; mechanical actor membership and Session layout remain covered.
No global inventory enumeration is added to ordinary mutation maintenance.
Trimming retains composite stamp dependencies; controlled REPLACE compatibility
also invalidates these fields. Unknown/malformed source changes cannot hide behind
a previously valid model. Inventory's JSON array is normalized to a tuple only;
strict scalar checking and duplicate-ID validation remain enabled.

All fields use the existing immutable owner replacements, SessionMemory budget,
UnitOfWork savepoints and durable-COMMIT/RAM-publication boundary. Versions come
from the caller's scope, never a fresh DB read or payload digest. Rollback restores
both fields and stamps, including repeated numeric candidate versions. Bounded
legacy content pools still support explicit unversioned fixed inputs; they cannot
select a current map on scene_id alone. Eviction, unload and recovery reconstruct
from persisted instances. Per-connection recovery generation and old-reader
publication fences are retained.

### Comparable deterministic evidence

`tests/test_versioned_runtime_inputs.py` measures actual production readers and a
real StepActor command. Baseline source is an archived, unmodified `e776fab1/src`,
selected explicitly by pytest pythonpath; both runs use the same locked environment
and test fixtures. The fourteen performance cases intentionally fail their new
assertions on that old source. Fixture correction/early implementation failures
are intermediate evidence only.

Growth 10 / 100 means that many distant static blocked cells, unrelated objects,
remote actors and another actor's inventory entries; the local target stays fixed.
Local/Light slices perform two repeated queries and one new local target; inventory
and sensory slices perform two repeated queries. The larger target-inventory slice
separately returns 11 / 101 entries (10 / 100 portable units plus one legacy entry).

| Slice / counter (10 / 100) | e776fab1 before | This follow-up |
|---|---|---|
| Local movement input: cache-key encoded blocked records | 10 / 100 | 0 / 0 |
| Same: full-map copies; sorted records | 1 / 1; 22 / 112 | 0 / 0; 12 / 12 local cells |
| Public single-cell Light: encoded blocked records; full Scene copies | 30 / 300; 3 / 3 | 0 / 0; 0 / 0 |
| Sensory map: encoded blocked records; full Scene copies | 20 / 200; 2 / 2 | 0 / 0; 0 / 0 |
| Target inventory output: encoded inventory records | 11 / 101 | 0 / 0 |
| Same: dependency version checks | 41 / 311 | 4 / 4 |
| After another actor's validated transfer: target RAM calls/rows | 18 / 108 | 2 / 2 |
| Same: target inventory records re-encoded; all version checks | 4 / 4; 33 / 33 | 0 / 0; 4 / 4 |
| Real StepActor: internal cache-key blocked records | 40 / 400 | 0 / 0 |
| Same: topology constructions; necessary full evidence encodings | 1 / 1; 1 / 1 | 0 / 0; 1 / 1 |
| Same: blocked records in persisted stage evidence | 10 / 100 | 10 / 100 (retained) |

Cache-lookup encoding call counts (equal at both sizes) are respectively:
local 1 -> 0, public Light 3 -> 0, small inventory 2 -> 0, sensory 2 -> 0,
full target inventory 1 -> 0, target after foreign transfer 5 -> 0, and movement
4 -> 0. Movement's one additional evidence encoding remains 1 -> 1; it is
separately classified, not removed or counted as a cache key.

SQL / RAM / checks are separate: local slice SELECTs/SQL rows = 3/0 and RAM
calls/rows = 10/2, checks 136, unchanged at both sizes; these include cold local
occupancy for the new target. Light uses 0 SQL, RAM 6/6 -> 3/3, checks 41 -> 44.
Sensory uses 0 SQL, RAM 2/2 -> 0/0, checks 0 -> 4. Small inventory uses 0 SQL,
RAM 4/4 unchanged, checks 30 -> 8. Full target inventory uses 0 SQL, RAM 2/2
unchanged. Movement SELECTs/rows = 6/2 unchanged, RAM 49/29 -> 46/26, checks
133 -> 137. Each figure is equal in the two growth groups. All warm-reader
slices have zero topology constructions and zero JSON-model parses before/after;
instrumented Python InventoryState/MechanicalGrid validation is also zero on
delivery warm reads. No-parse alone was insufficient: old encoding counts above
remain nonzero. Legitimate output copies/serialization and cold preparations are
not relabeled as cache lookup.

Dependency-check figures count actual SessionMemory.version calls in both runs,
not SQL or scalar CPU comparisons. The inventory token reads a fixed four
version components plus the existing memory generation; its comparison does not
enumerate inventory entries or a variable dependency collection.

Prior field-maintenance cases still show weight 1 check, no mechanism index copies
or rediscovery, and fixed unrelated growth. New record validity raises mechanism
command checks 111 -> 114 (RAM 78/32 -> 76/30); four bag commands checks
972 -> 1000 (RAM 284/180 -> 280/176), identically at both sizes. Their thresholds
track this measured constant overhead; the original dated figures above stay
unchanged. No work was shifted into an added full-scene scan.

### Validation and retained boundaries

Final targeted suite: 47 passed, exit 0, including all 22 new cases plus prior
owner/field-maintenance tests. New cases cover distinct item/owner selection keys,
unprojected invalid source rejection, multi-stage map/inventory changes, savepoint
and whole rollback, repeated candidate stamps, old fixed maps, full sensory
requirements, unload/restart, and map commit failure/unknown commit/publication
failure with recovery on another connection. Existing four publication-failure
modes now also assert old/current inventory model isolation. Final complete-regression
and documentation validation results are recorded below.

Remaining serialization is deliberate: persisted facts/deltas, receipts, evidence,
protocol snapshots, idempotency fingerprints, source bindings, existing JSON
decoders and unversioned external/fixed maps. Casting/effect selections retain the
pinned strict JSON-model compatibility route when rule_components actually rebuilds;
its effective outer cache still reuses valid results. Complete inventory/evidence/
projection outputs retain proportional output work. Cold aggregate ComponentInputs
index preparation and SQLite aggregate inventory UPDATE retain their existing
aggregate storage cost; the measured target read after an unrelated domain patch
does not reconstruct them. Unattributed aggregate SQL remains conservatively
invalidating. Actual light-source/door and scene-passage dependency checks remain;
this is not a constant-cost claim across growing relevant geometry or sources.

NPC all-actor reads and transaction-wide version-dictionary copying are unchanged,
separate tasks. The independently reported **caught SQL statement error followed
by continued commit can diverge RAM/SQLite** issue remains **unresolved and outside
this acceptance**. Cache/commit fault tests here do not establish its repair.
No general map-edit command, live stack editing, multiplayer load guarantee or
broader recovery capability is introduced.

### Final managed-input acceptance

Full locked-environment regression: **2,091 unique tests**, disjoint sorted-file
partitions **665 / 757 / 669**, all exit 0. Collection/JUnit union has no missing,
unexpected, duplicate, failed, errored or skipped cases. All 216 Python source,
test and example hashes are unchanged during the final run. Historical migration
tests are included; no environment-blocked case is reported as a pass. The final
47-test targeted run also exits 0 and is not added again to the full-suite count.

Actual commands use `uv run --locked --no-sync pytest -q`, repository-local unique
basetemps, JUnit output and `-o faulthandler_timeout=60`. The focused files are
`test_versioned_runtime_inputs.py`, `test_runtime_instance_ownership.py` and
`test_runtime_field_maintenance.py`; the full suite assigns every sorted test file
to exactly one of three modulo partitions. Evidence: `.test-tmp/versioned-final-target.*`,
`versioned-delivery-{0,1,2}.*`, `versioned-delivery-collection.txt`,
`versioned-delivery-audit.json` and `versioned-counts.json`. Old-source runs are
`versioned-before-comparable.log` and `versioned-before-commands.log`.

`uv run --locked --no-sync ruff check src tests examples` and
`uv run --locked --no-sync ruff format --check src tests examples` pass (216 files).
Both repositories' changed Markdown links/anchors/fences pass: 13 files, 170 local
links; staged/unstaged whitespace checks pass. Only two existing Lark deprecation
warnings occur per pytest group. No dependency synchronization/upgrade, remote CI
or sustained multiplayer load was performed. The explicit independent SQL-error,
NPC, aggregate storage and genuine dependency limits above remain open.
