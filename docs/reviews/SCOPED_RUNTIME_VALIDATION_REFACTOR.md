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
SQL filtering, Schema40 and initial real-path regressions. Phase 2 adds the
full-suite compatibility repairs, additional scale evidence and this documentation.
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
share dynamic memo values. It reuses actual positions, definitions, eligibility,
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
| 20 | Fixed: Package-lifetime index `(event_type, scene_id)` preserves definition order | Events/traceability/conditional effects/cascade/rollback suites; trigger history and 32-consequence limit unchanged |
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
| GM placement availability, cold GridMaps, baseline fixture | 95 / 91 | 22 / 17 | definition models 23→1; returned string bytes 38440→8442 |
| Same target, +100 unrelated objects (half same scene), +100 remote Actors, 34 ground portable instances | 645 / 641 | 22 / 17 | definition models 173→1; returned string bytes 161553→8442 |
| PutItem, command already constructed, initial owned key→ground container | 515 / 545 | 158 / 133 | inventory UPDATE 2→1; owner UPDATE 4→1; all app UPDATE 9→5 |
| Authorized list with container/carried emitter fixture | 377 / 354 | 122 / 107 | actual Light.project preparations 6→1 |
| Real ordered mechanism cascade | 1863 / 1835 | 276 / 221 | index builds 4→1; all app UPDATE 8→5 |
| Task recovery, original fixed grant and no extra expired history | 13 / 19 | 13 / 19 | returned string bytes 4021→4021 |
| Same Task, +30 actual arrival/departure observation batches, all expired | 13 / 79 | 13 / 19 | returned string bytes 20611→4021 |

The last four after-counts were measured before final mechanical identity-closure
hardening; final repeated counts, including any constant overhead, are recorded
with final verification below. The placement scale assertions also count bytes,
so merely returning one large unrelated configuration row does not satisfy them.
Container closure is separately asserted to contain exactly the two relevant
nodes while 100 unrelated persisted objects and Actors exist. Complete scene
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
constant-cost result here. Capacity may legitimately load large ancestor subtrees;
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
