# Scoped runtime validation refactor — 2026-10-20

## Authority, baseline and delivery scope

Architecture Decision Status: **[DECIDED]** by sibling Module Contracts §2.3,
§2.4 and §6.5–§6.6. This report records implementation and evidence; it does not
create another architecture standard. State Machine Architecture §3.3/§5 and
World Subsystem Design §2/§5/World local validation apply. Their current contents,
their AGENTS.md, local domain documents and the dated runtime review were read.

Starting repository: `D:/Projects/agentic-trpg/trpg-state-machine`, remote
`https://github.com/agentic-trpg/trpg-state-machine.git`, requested branch
`refactor/scoped-runtime-validation`, HEAD and local origin/main
`7927a382738fc97e2420967c3e1c6280c717d1fb`. The original untracked text document
was preserved. Implementation uses an isolated `.test-tmp/scoped-runtime-validation`
worktree on `codex/scoped-runtime-validation`; delivery advances only the requested
feature branch. No main merge, main push, sibling modification or RE upgrade.
The installed Engine/SRD pin stays `a011eba3ea2226e22d8081294ee38d3ec193b325`.

Phase 1 commit: `4aa60a0` — scoped domain admission, transaction-local reuse,
SQL filtering, Schema40 and initial real-path regressions. Phase 2 (`dd828c2`) adds full-suite compatibility repairs, additional scale evidence
and this documentation. Phase 3 (`0072d84`) closes the final Scene/port metadata callers: one
transaction-stage existence query and SQL-filtered port DTOs by Scene/Object/Passage.
Only complete granted navigation layouts retain unfiltered fixed port input.
Final validation and delivery commits are recorded below after execution.

## Responsibilities and invariant coverage

| Responsibility | Concrete implementation | Cost and permitted consumer |
|---|---|---|
| Full spatial audit | `WorldHandler._assert_spatial_consistency`, `ObjectSpatialService.audit`/`_check_scene` | Complete instantiated spatial scope, only creation acceptance, save/resume or explicit trusted diagnostics |
| Structural acceptance | `SceneLifecycleService._validate_scene`/`validate_initialized`, `runtime_integrity.certify_scene` | Declared objects/configs/capabilities/locks, initial Actor identities and maps; initialization, upgrade/migration and recovery |
| Local spatial admission | `ObjectSpatialService.check_candidate`/`_check_shape`/`_touching`, `GridService.local_topology` | Complete candidate shapes, bounds, internal wall edges, reserved passage cells, actual intersecting blockers/Actors and every candidate intermediate state; local edges include diagonal corner cells |
| Container admission | `ContainerService._rows(roots)`, `audit(affected=...)` | Moving subtree, old/new ancestors and their descendant aggregates needed for weight/capacity, fit, two-container depth, cycles and access |
| Mechanical input admission | `CharacterFacts.rule_components(owner_ids)` | Selected owners plus relevant instance identity collisions/orphaned equipment references; typed Schema and portable mirrors still validated |
| Inventory projection | `prepare_inventory_change`, `project_item_transfer`, `sync_inventory_change` | Validate old subtree/owners before mutation; update changed portable entries and old/new owner mirrors once, including access changes |
| Shared finalize | Existing CommandRuntime, RuleExecution, SessionRuntime and Store | Authorization, OCC, binding, idempotency, RNG, accepted result admission, events/evidence/outbox/receipt and common commit/rollback; no default spatial audit/inventory rebuild |

The spatial candidate algorithm checks all supplied replacements together, ignores
replaced old poses when testing conflicts, and reads transaction-visible indexed
blockers. Occupancy and movement blocking remain separate; difficult terrain,
vision, cover, rain and wind are not collapsed into this index. Shared doors keep
both endpoint/threshold checks and fixed-placement restrictions. Ordinary damage
only changes HP; a destruction transition additionally validates its spatial
policy, updates contained/carried access and synchronously applies component
breakage. Existing container destruction keeps contents and exposes the destroyed
shell; no new drop/random destruction rule was invented.

Schema40 adds rebuildable `object_spatial_cells` (indexed by Session/Scene/Cell),
`scene_integrity` and its required-row dependency index, plus an Actor cell index.
SQLite triggers maintain derived cells for pose/state/config/HP changes and
invalidate structural acceptance on required-row deletion/config changes. These
are derived lookup/admission data, not a second editable World authority. Fresh
creation certifies only after objects, NPCs, items and locks are installed; delayed
creation certifies its completed scope. Existing-scene re-entry reads the
certificate and map, never replays templates or performs `_validate_scene`.
Incomplete instances fail closed until explicit source-checked upgrade/acceptance.
Migration builds the derived records from existing facts, without resetting facts,
bindings, versions, RNG, history or receipts. On open, the exact maintenance
schema/trigger definitions must be present. External writes still require the
Store-installed SQL functions, as existing environment maintenance already does.

`scoped_read` belongs to the existing UnitOfWork, retains the reader object rather
than an address-only key, and expires on every mutation stage using SQLite
`total_changes`. All raw command/rule savepoint rollback paths and Store savepoints
clear it; commit/rollback destroys it. New transactions and old WAL readers never
share dynamic memo values. It reuses immutable raw object rows, carrier positions, prepared Scene inputs, definitions, eligibility,
spatial contributions, light preparation and view inputs within one stage;
authorization and observer filtering remain at each public entry. FrozenRuleset
and content-keyed PreparedGrid remain unchanged. Navigation's explicitly static
Package topology is kept separate from mutable live map input.

## Checklist acceptance

Every row was checked against production callers, not only function names.
“Fixed” describes the existing supported behavior, not unsupported new mechanics.

| ID | Status and implementation | Evidence and remaining boundary |
|---|---|---|
| 01 | Fixed: commands/rule_execution no longer call `_assert_spatial_consistency` on finalize | `test_scoped_runtime_validation`, Openable consistency, rules admission/atomic commit suites; local domain admission precedes shared publication |
| 02 | Fixed: eventful commands no longer unconditionally call full inventory sync | Flag command has zero full audits/sync/index builds; World mutation owners maintain projections |
| 03 | Fixed: candidate shape and indexed conflict closure replace `_check_scene`; `operate` removes post-audit | ObjectSpatial/WorldSpatial tests cover rotation, walls, reservations, candidate overlap, SQL failure and replay |
| 04 | Fixed: placement availability uses the same read-only local candidate admission | Public placement query scale test: 0 vs 100 unrelated objects/Actors/items, same returned range/model count |
| 05 | Fixed: spatial view reads `legacy_object` by visible ID | Instance containment/template-prohibition and spatial view tests; explicit scene output still enumerates its actual visible candidates |
| 06 | Fixed: list seeds actual local ground roots, owned roots, descendants and both-sided doors; shares UoW input | Container/light/hidden-content/query tests, one light preparation; each operation retains its own eligibility rules |
| 07 | Fixed: `operate_open` uses transition admission and subtree access projection, removes global audit | Ordinary/shared door and container/light regressions; no combat/magic opening extension |
| 08 | Fixed: `operate_bar` retains local bar/side/occupancy admission, removes global audit | Openable boundary/review/lock regressions; closed-shell bar changes do not rebuild inventory |
| 09 | Fixed: check auto-open goes through one transition path; outer check and rule finalize audits removed | Real ForceOpen/PickLock, failed accepted checks, conflict, RNG and after-state spatial rejection/rollback |
| 10 | Fixed: nonfatal HP does not audit/sync; destruction handles spatial/access/component consequences locally | Durability, spatial durability and mechanism component rollback tests; real player object attacks remain unsupported |
| 11 | Fixed: `ensure_scene` reads structural acceptance for existing scenes | Missing dependency invalidates certificate, savepoint rollback restores it, save/resume rejects incomplete facts; complete acceptance still runs on creation/upgrade/recovery |
| 12 | Fixed reads; baseline already had adjacency/weight memo and scoped affected audit | Recursive indexed loading of moved subtree, old/new ancestors and necessary sibling aggregates; capacity/unknown/cycle/nesting tests retained |
| 13 | Fixed: source selection stays in effective scene (ground/carried/nested); one projection preparation per read stage | Light tests cover container transmission, destroyed shells, carried sources and disclosure. Full scene lighting output legitimately visits scene blockers and cells |
| 14 | Fixed: irrelevant types/phases filtered before index; one lazy configuration-only index per expansion batch | Real ordered cascade builds 4→1; damage component consequence runs synchronously once. New batches/retries rebuild; no cross-transaction mechanism cache |
| 15 | Fixed: `_area` checks requested scene record rather than decoding all Scenes | Environment area/source/boundary/provider/authorization regressions; source validation remains in each current input scope |
| 16 | Fixed Python/input scope: SQL projects owners before DTO construction/portable validation | Mechanical, history-free, inventory and availability tests; relevant identity/orphan-equipment protection remains. Aggregate storage JSON still costs SQL traversal |
| 17 | Fixed: item transfer prepares old subtree and updates new/old owners incrementally | Nested Put/Take/Pickup/Drop, bound/equipped/material refusals, access, quantity/charges and transaction tests |
| 18 | Fixed: mutation owner projects once; generic finalize no longer repeats it | Real PutItem inventory UPDATE 2→1, owner UPDATE 4→1 in measurement fixture |
| 19 | Fixed repeated Python owner scans and duplicate loads: owner/identity sets and by-owner slug index; `require_unbound` uses relevant actor once | Inventory output order and all supported binding restrictions retained; aggregate JSON encoding is an explicit storage cost |
| 20 | Fixed: shared Package-lifetime trigger and definition-ID indexes preserve definition order | Real dispatch plus fired-signal capture enumerates definitions once across three flag commands; events/traceability/conditional effects/cascade/rollback suites; trigger history and 32-consequence limit unchanged |
| 21 | Fixed: recent/pending/IDs/occupancy/limits down to SQL; metadata omits content; Task unions fixed Work/Recent grant once | Observation/Gate/EVA/mode/output/perception suites; TTL, cursor, ACK, recovery, epochs and redelivery policy unchanged |

## Deterministic measurements

Both versions used the same real fixtures, pinned RE and transparent connection /
cursor observers. Before source is exactly `7927a38`; after source is this branch.
Queries are executed, not mocked. SQL SELECT/CTE counts are application-issued
statements; rows count fetched result rows (including repeat reads), not distinct
entities or SQLite VM visits. Trigger-internal instructions, index B-tree work,
SQLite JSON scans/encoding and Python container iteration are not mislabelled SQL
statements. The model column counts actual tagged `WorldObjectDefinition` decoder
construction, not every dataclass/deepcopy/allocation. Dynamic indexes are real;
`EXPLAIN QUERY PLAN` uses `spatial_cell_lookup`.

| Real path / condition | Before SELECT / rows | After SELECT / rows | Other measured work |
|---|---:|---:|---|
| GM placement availability, cold GridMaps, baseline fixture | 95 / 91 | 21 / 16 | definition models 23→1; returned string bytes 38440→7886 |
| Same target, +100 unrelated objects (half same scene), +100 remote Actors, 34 ground portable instances, +20 lazy Scenes and 10 unrelated passages | 645 / 641 | 21 / 16 | definition models 173→1; returned string bytes 177193→7886 |
| PutItem, command already constructed, initial owned key→ground container | 515 / 545 | 122 / 97 | inventory UPDATE 2→1; owner UPDATE 4→1; all app UPDATE 9→5 |
| Authorized list with container/carried emitter fixture | 377 / 354 | 65 / 50 | actual Light.project preparations 6→1 |
| Real ordered mechanism cascade | 1863 / 1835 | 223 / 168 | index builds 4→1; all app UPDATE 8→5 |
| Task recovery, original fixed grant and no extra expired history | 13 / 19 | 13 / 19 | returned string bytes 4021→4021 |
| Same Task, +30 actual arrival/departure observation batches, all expired | 13 / 79 | 13 / 19 | returned string bytes 20611→4021 |

The application-path counts above were repeated after final mechanical identity,
Scene/port closure and raw-row reuse changes; the Task recovery counts are also
asserted by the deterministic regression suite. The placement scale assertions also count bytes,
so merely returning one large unrelated configuration row does not satisfy them.
Container closure is separately asserted to contain exactly the two relevant
nodes while 100 unrelated persisted objects and Actors exist. The same PutItem
measurement separately profiles real `ContainmentState` memory operations: rows
consumed by adjacency construction are 7→2, mapping lookups remain 16→16 and
child rows returned by `contents` remain 1→1. These include repeated accesses and
membership lookups, not SQL executions or all Python memory traffic; none of the
observed implementation is bypassed. The transparent profiler restores the
previous profile hook. `tests/runtime_scope_measurements.py` reproduces both SQL
and memory counts with `PYTHONPATH=<revision>/src;<this-worktree>/tests` and a new
fixture output directory, using the same locked interpreter. Complete scene
lists/evidence and required ancestor/owner closures are allowed to grow with
actual output/dependencies. No zero-SQL, zero-allocation or universal constant-cost
claim is made.

## Compatibility and explicit limits

Public command/query DTOs, bindings, fingerprints, receipt/replay formats and RE
pin are unchanged. Schema39→40 is additive and transactional; this branch is not
a backward-open promise for older binaries. Historical Schema38/36 saves retain
their existing explicit instance-upgrade and evaluator-mismatch boundaries. Full
save/resume checks still reject unsupported active combat and incomplete state.

Mechanical components remain one authoritative aggregate JSON row. SQL filtering
reduces returned data/Python validation and owner writes, but SQL JSON traversal
and an updated aggregate's encoding still depend on stored content. Replacing
that representation would require a separate storage migration, not an invented
constant-cost result here. The aggregate Session port JSON also retains SQL-side traversal; only matching
ports are decoded/returned. Capacity may legitimately load large ancestor subtrees;
owner inventory validation may require the owner's complete inventory; full light
projection and stage evidence may require complete scene inputs. No generic dynamic
cache or extra gameplay version fields were added.

World-source RE effects, dynamic vision, real player object damage, random container
destruction/drop mechanics, equipment/effect lifecycle transfers and previously
unsupported combat world operations remain outside the implemented capability
surface. Existing refusals are retained, not counted as feature completion.

## Verification ledger

Intermediate focused evidence (not added as disjoint totals):

- Core ObjectSpatial/Container/Light/Openable/Scene/Mechanism run: 101 passed,
  1 historical-producer case deselected, exit 0.
- NPC retention/EVA/Gate/modes/tasks first run: 90 passed, 1 historical fixture
  failure, exit 1; the old fixture now performs explicit instance-config upgrade.
- Stage review: 55 passed, exit 0.
- Complete affected-file repair run (Grid/maps, history-free mechanics, instance
  migration/containment, mechanism migration and navigation boundaries): 191 passed,
  exit 0.
- Initial new deterministic suite: 10 passed, exit 0; later added StepActor growth
  case is included in final collection below.
- Ruff check, Ruff format check (206 files at that time), git diff --check: exit 0.

Tests use the existing locked `.venv`, repository-local temp/cache and explicit
worktree `PYTHONPATH`; no dependency update. Fast commands are
`uv run --locked --no-sync ruff check src tests examples` and corresponding
`ruff format --check`. Pytest is invoked through that locked environment's Python
(`python -m pytest -q ... -o faulthandler_timeout=60 --basetemp=...`). Early sandbox
uv-cache persistence errors and pytest stalls were infrastructure runs, not passes;
the actual behavior runs use the same environment outside that restriction. An
initial too-long historical Git checkout was retried with process-local
`core.longpaths=true`, without changing Git configuration or dependency pins.

A preliminary full run was deliberately exploratory while compatibility repairs
continued; it is not the final-source gate. It found the explicit initialization
acceptance fixture, old null time_wait fingerprint fixture, static/live navigation
map distinction, relevant orphan equipment detection, original missing-config error
code and synthetic old-schema reconstruction issues. Repairs retain original
fact-preservation, invalid-state, rollback, authorization and identity assertions.
Final frozen-source results and remaining failures are appended after execution.


### Final source-scope follow-up

The final indirect-call pass also migrated NPC Result target-scene admission to
`InstanceData.has_scene`, shared with Environment source validation in one stage.
`InstanceData.passages` filters before decoding by actual Scene/Object/Passage;
shared-door callers still receive both endpoints. The scale fixture additionally
adds 20 complete lazy Scene plans and 10 unrelated two-sided passages. Missing
Session configuration fails explicitly rather than becoming an empty port list.
The 78-case Scene/port/navigation/protection combination and 84-case
Environment/NPC-result combination passed (exit 0; the latter excluded one
process-named case only for the interim focused run). Complete coverage below
includes every collected case. Interrupted preliminary partition runs are not
counted as final passes.

The last structural pass includes legacy door-lock dependency invalidation and a
Session-configuration existence check on Scene entry. Full read-only save validation
also rejects invalidated structural acceptance. Historical producer fixtures now
explicitly target their own `.venv` rather than inheriting an outer
`UV_PROJECT_ENVIRONMENT`. A detected old-RE environment was restored from the
unchanged current lock before repeating validation. Its initialization failures
are environment failures, not passing or product-regression evidence.

### Frozen production and fixture follow-up

Production code is frozen at `d4e88dc42711f0a3c0e852ef12733d424421442f`.
The final 40-case scoped/Scene/migration combination passed (exit 0). The new
legacy-lock acceptance check exposed two manually wired historical-style fixtures
that accepted a Scene before installing its locks. Neither failure was suppressed:
the direct-domain seed now installs locks before acceptance; the lazy Grid fixture
installs only its instantiated Scene's required locks, leaving the destination
uninitialized and retaining every no-write/fact-preservation assertion. All five
files importing the shared seed passed together (213 cases, exit 0); the entire
GridService file passed separately (23 cases, exit 0). The two affected incomplete
partition runs were stopped after diagnosis and restarted; they are not passing
full-suite evidence. No production code changed for these fixture repairs.
The extra transparent memory profiler is an uncollected measurement utility,
not a replacement implementation or test bypass.

### Reproducible final commands and artifact scope

The complete collection is split greedily by per-file case counts into 29/30/30
files and 672/672/671 cases. Each file belongs to exactly one partition. Ignored
local manifests and JUnit logs are retained under the isolated worktree's
`.test-tmp/`; they are verification artifacts, not shipped production state.
Manifest SHA256 values:

- `release-p0.json`: `c2862c60ebcc5fc1fd8265235debea9f85f156acf4849e9c5c5d50a512af6e5f`
- `release-p1.json`: `0d601245e33c455490816254e60b32fb55dbd0fc63fda9f6bab953733e92c9b3`
- `release-p2.json`: `311d9aaf420d50d5f668f40567a7c132246f42950ddd047ed1e21c6d9b7a8434`

Commands run from the isolated worktree in PowerShell, with the original locked
`.venv` and repository-local cache/temp (no dependency or lock update):

```powershell
$env:UV_CACHE_DIR='D:\Projects\agentic-trpg\trpg-state-machine\.uv-cache'
$env:UV_PROJECT_ENVIRONMENT='D:\Projects\agentic-trpg\trpg-state-machine\.venv'
uv run --locked --no-sync ruff check src tests examples
uv run --locked --no-sync ruff format --check src tests examples
uv lock --check --offline
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
$env:GIT_CONFIG_COUNT='1'
$env:GIT_CONFIG_KEY_0='core.longpaths'
$env:GIT_CONFIG_VALUE_0='true'
# Reproduce the three independently launched final commands serially.
foreach ($taskIndex in 0..2) {
    $taskPrefix=if ($taskIndex -eq 1) { 'release' } else { 'release2' }
    $taskTemp=if ($taskIndex -eq 1) { 'release1' } else { "release2$taskIndex" }
    $taskFiles=Get-Content ".test-tmp/release-p$taskIndex.json" -Raw | ConvertFrom-Json
    & 'D:\Projects\agentic-trpg\trpg-state-machine\.venv\Scripts\python.exe' -m pytest -q @taskFiles "--basetemp=D:/Projects/agentic-trpg/trpg-state-machine/.test-tmp/$taskTemp" "--junitxml=.test-tmp/${taskPrefix}-p$taskIndex.xml" -o faulthandler_timeout=60
}
```

The same interpreter also ran `-m pytest -q` with the complete focused files
listed above and `--basetemp=<repository>/.test-tmp/complete-seed` (213 cases),
`lazy-grid-complete2` (23 cases), and `freeze-guard5` (40 cases). These overlap the
complete collection and must not be added to its total. Baseline/current work
measurements ran `tests/runtime_scope_measurements.py` with separate new output
directories and the corresponding revision's `src` on `PYTHONPATH`.

For failure accounting: the first exploratory full run recorded 1989 passed /
19 failed (exit 1), before the compatibility and scope repairs. A mistyped
focused file selection exited 4 without running tests. A later contaminated
old-RE environment produced initialization failures (40 failed, exit 1) and a
measurement import failure (exit 1); after exact locked reinstall, the guards
exposed fixture/assertion issues (36 passed / 4 failed, then 39 / 1, exit 1) before
the final 40-case pass. The first lazy-Grid fixture correction still attempted to
initialize locks for an uninstantiated other Scene (22 passed / 1 failed, exit 1);
the corrected instantiated-scope fixture passed all 23. Cancelled preliminary
partitions are not passes. These runs are retained as diagnostics, not combined
with final passing totals. No GitHub Actions result is inferred from local runs.

### Final executed results — complete collection

| Run | Files / cases | Result | Exit |
|---|---:|---|---:|
| `release2-p0` | 29 / 672 | 672 passed; 566.83 s | 0 |
| `release-p1` | 30 / 672 | 672 passed; 639.94 s | 0 |
| `release2-p2` | 30 / 671 | 671 passed; 528.61 s | 0 |
| Final complete collection / JUnit identity check | 89 / 2015 | 2015 unique passes; 0 failures/errors/skips; exact node-ID union | 0 |
| Shared-seed dependent files | 5 / 213 | 213 passed; 56.94 s | 0 |
| Lazy Grid fixture and entire GridService file | 1 / 23 | 23 passed; 13.53 s | 0 |
| Final scoped/Scene/instance-migration combination | 3 / 40 | 40 passed; 22.38 s | 0 |
| Ruff check / format | src, tests, examples / 207 files | passed | 0 |
| `uv lock --check --offline` | existing lock / 19 packages | passed | 0 |
| Changed Markdown links/anchors/fences | 20 files / 91 local links | passed | 0 |
| `git diff --check` | complete branch diff | passed | 0 |

The three complete partitions are disjoint; focused cases above overlap and are
not added to 2015. Partition 1 ran at `d4e88dc`; partitions 0/2 at
`4ee6426779db5dcc0efd131a0edc095f5f797fde`. Production `src`, `pyproject.toml`
and `uv.lock` are byte-identical between those commits. The fixture changes
relevant to partition 1 were additionally covered by all 213 shared-seed dependent
cases. The final commit only records this evidence. The complete collection is
not presented as a single monolithic invocation or a GitHub Actions result.
Third-party Lark `sre_parse`/`sre_constants` deprecation warnings remain.

JUnit SHA256 (artifacts retained in the isolated worktree):

- `release2-p0.xml`: `e348467d2f093667d9ef67f323518bed1c5e312d238526b02ff12de09fc343ab`
- `release-p1.xml`: `2fd2d2cd81d52d0566b0ce942048440a8a1022ef0f07f2a37dc3924dfb67d076`
- `release2-p2.xml`: `e05580ffa463f2affd3d1ddc41854df54c27da1549779adbffa6e02f25c370e7`

Implementation/delivery commits are `4aa60a0`, `dd828c2`, `0072d84`, `d4e88dc`
and fixture/measurement commit `4ee6426`, followed by this evidence-only commit.
The original requested feature branch is fast-forwarded from `7927a38` only after
these checks and pushed normally to `origin/refactor/scoped-runtime-validation`;
the final delivery response records the exact resulting SHA and push verification.
The isolated worktree and local diagnostics are retained for review. The original
untracked empty text document remains excluded and unchanged (SHA256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).

The architecture repository remains at `bdbbdf31cc60f3e83a951541ddf7a70504526190`
and RE at `f18e0ad0d0bb6641efb050b51bf919dd41a8631f`; their pre-existing untracked
files are untouched. Installed Engine and SRD still resolve to
`a011eba3ea2226e22d8081294ee38d3ec193b325`. No remaining test failure or external
blocker is known in this scoped delivery. Aggregate-JSON and necessary dependency
costs, plus the unsupported gameplay capabilities listed above, remain explicit
limits rather than completed features.
