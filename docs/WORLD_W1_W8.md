# World W1–W8 implementation record

2026-10-22 后续：Object.container 持有直接内容物、自身重量、内容物重量和含后代总重量；
公开 view/list 直接使用这些字段，不构建 ContainmentState 或全候选键。隔离候选容量仍检查
移动子树及两端必要祖先。Scene.lighting 的 sources／geometry／cells／projection 已接入
单格公开照明、Grid topology、非战斗 RE Snapshot 和阶段证据；显式完整地图及战斗固定地图
仍保持对应输入含义。场景 objects.direct 不混入携带／嵌套物品，贡献关联另沿实际位置解析。
这些是当前分支后续实现；历史 W1–W8 数字不重复计入。本次验收与限制见
[实例归属报告](reviews/RUNTIME_INSTANCE_OWNERSHIP.md)。

Baseline: `1fa7cea5dab7bb95f3f6fa975f8e681c691937a6`; remote main verified at that
commit on 2026-10-15. Implementation uses an isolated clone on `feat/world-w1-w8`
under `.test-tmp/world-w1-w8`. The original checkout's uncommitted GC5 changes are
excluded and preserved. No AGENTS.md, check-fast, or CI workflow exists at this
baseline. Checks use pytest, Ruff check/format and git diff --check.

Architecture read-only reference: `agentic-trpg@acb446bb97241aa32e0d12c35b02bf30dd7d9eae`,
MODULE_CONTRACTS §0.3–0.6 and §3–6, WORLD_SUBSYSTEM_DESIGN. Candidate ABI examples
are not implemented protocols. Session, WorldFacts, CharacterFacts, Activity,
Effects, CombatRecords and Perception keep their existing authority boundaries.

## API baseline and shared contracts

Writes use `submit_command(TypedCommand, package, context=TrustedContext)` and
`submit_command_async`; `sm-command/1`, existing fingerprint, expected world
version, receipts and savepoint/transaction. Context is authenticated by caller
integration, never parsed from payload. Rejected/unsupported actions produce
rejected receipts with error_code; stale versions produce conflict/version_conflict.
Storage errors roll back the whole transaction. Replays use existing command receipts.
Queries are synchronous detached reads. `get_command_receipt` supports lost replies.
There is no HTTP server or authentication implementation in this library.

Legacy player/NPC command paths: StepActor, TraversePassage, LockDoor/UnlockDoor,
PickupItem/DropItem, navigation activities; NPC Evaluation has explicit existing
intent conversion. New operations will document NPC conversion separately.
All World changes retain active-combat guards. Queries do not authorize execution.
SetObjectState and PackageEffects are audited with each capability extension.

## W1 — explicit definitions and authorized queries

Package `/6` adds immutable `WorldObjectDefinition` records in `world_objects`.
Unknown capabilities, duplicate capabilities, invalid identity references and
contradictory initial open state are rejected during complete Package validation,
before any session initialization. Only implemented openable is currently declared.
Legacy `/1`–`/5` digest and loading behavior remain unchanged; legacy openable/item
capabilities remain enabled through their existing contracts.

Public synchronous queries: `get_object(sid, actor_id, object_id, package, context=...)`
and `list_scene_objects(sid, actor_id, package, context=...)`. Actor control/NPC access,
public scene policy and actual visibility filter objects. Credential lists are absent.
Same capability works across declared object identities; names do not select behavior.

| Suggested fields | Actual location | Class | Writer | Consumer/status |
|---|---|---|---|---|
| session/object/instance ID | sessions + objects.object_id | R | existing initialization | same portable identity, queries |
| definition/template ref | WorldObjectDefinition.definition_ref | T/R | Package validation | WorldFacts.object_definition, object view |
| package/definition revision | sessions.package_digest/version | T/R | Session creation | pin checked on every entry; no redundant per-object revision |
| display_name, description_ref | WorldObjectDefinition | T | Package | authorized query |
| material, size_category, weight_lb | WorldObjectDefinition, nullable unknown | T | Package | query; no material-derived mechanics |
| capabilities | tuple of strict capability dataclasses | T | Package | capability routing; unknown rejected |
| initial placement | ObjectDefinition.initial_cell / PortableItem.initial_owner_id | T | Package | existing one-time initialization |
| scene/cell/carrier | objects; actors for carried origin | S/R | WorldFacts + CharacterFacts | single actual location |
| effective_capabilities/object_view | WorldObjectService.view | D | none | authorized queries; no state copy |
| object_kind | deferred | T conditional | none | no classification consumer in W1 |

No DB migration is necessary for fixed definition binding: Session already pins
the complete Package and object identity resolves within it. Restoring does not
reapply initial values. Creature identity stays with actors/CharacterFacts.

Validation: `pytest tests/test_world_definitions.py tests/test_lockable_passages.py -q`
(with checkout-local basetemp). Includes a separate Python process restore,
unauthorized query, strict validation and legacy lock/navigation compatibility.
Initial environment-only failures were fixed by selecting a writable basetemp.


## W2 — object durability and internal application

W1: 24 tests passed; commit 5d7a5d3 pushed. Pinned RE 675552406e67470bf3456fe90e88c4f1595bf324
(Engine 0.6.0, snapshot/17): CombatObject contains holder/placement attestations, no
HP/AC. RulesAdapter.delta_changes limits HP targets to character_states. Player
object attacks, repair and combat world operations are unsupported.

ObjectDurabilityService._apply_admitted is a private participant inside an admitted
caller's shared transaction. Existing private execution origins carry provenance;
no player command/parser/StateMachine write API accepts this result or a validated
flag. No fake adapter exists. Internal synthetic fixtures prove application only.

| Fields | Actual model/storage | T/S/R/D | Writer / consumer |
|---|---|---|---|
| durability, AC, max/initial HP | strict Durability config | T | Package / init, query, bounds |
| damage profile, optional threshold | explicit scenario policy + nullable threshold | T/R | Package / future RE input; no World formula |
| current_hp | object_durability | S | WorldFacts.apply_object_hp / settlement and query |
| destruction_policy_ref | disable_preserve_obstruction | T | Package / identity and obstruction retained, operation disabled |
| destroyed, usable | HP==0, effective_capabilities | D | derived / authorized view and legacy switch guard |
| resolution/source/snapshot | private typed settlement, existing world_version | R | internal admitted caller / stale and target checks |
| repeated application | world_result_adoptions | R | same shared transaction / fingerprint and dedup |

DB 25→26 adds empty tables. Old package/state/IDs/quantity/charges remain untouched.
Combining excluded GC5 later requires explicit renumbering of its independent
migration 26. Full PHB/DMG/MM texts were not verified. Rules checked in
[SRD 5.2.1](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf):
p.178 Breaking Objects and p.180 Damage Threshold. Fixture AC/HP are explicit
scenario values; material does not generate them. Threshold is not subtraction.

Checks: W1+W2+legacy lock/navigation suite: 26 passed. Includes synthetic internal
rollback, replay, stale/session/bounds rejection, destruction and reopen. These do
not prove a genuine RE object attack path. Later batches must explicitly integrate
destruction consequences for container/light/spatial combinations before accepting them.


## W3 — independent open, jam and bar facts

W2 commit d6cdfe2 pushed. W3 keeps objects.state as the only open/closed authority.
Package Openable config specifies initial jam/bar, prevent_transitions jam policy,
operation scene sides and bar sides. Absent bar is null, distinct from unbarred.
Legacy definitions preserve previous behavior. DB 26→27 adds object_openables;
old saves receive no invented jam/bar capability. Facts never commit.

| Fields | Actual location | Class | Writer / consumer |
|---|---|---|---|
| initial open state/open_state | ObjectDefinition.state / objects.state | T/S | init, WorldFacts.switch / all door and navigation consumers |
| initial_jammed/jammed, initial_barred/barred | Openable / object_openables | T/S | init, set_object_bar / switch inhibitors |
| jam/bar/access policies and sides | Openable typed config | T/R | Package / WorldHandler target admission |
| passage/door/endpoints/standing/threshold | existing PassageDefinition | T/R | Package / shared two-sided door, navigation |
| can_open/close/bar/unbar | authorized object view + same pure validators | D | derived / callers |
| passage availability | existing _door_closed + latest topology | D | derived per step / Activity execution |

Open/Close use existing SetObjectState; BarObject/UnbarObject are registered in the
closed command union/parser, authorization, query and noncombat dispatch. Sync and
async submission supported. Player and explicitly controlled NPC may submit typed
commands; NPC Evaluation intent conversion for bar is not implemented. ForceOpen,
Unjam and combat interactions remain unsupported. Existing NPC open/close intents
and navigation still use their established conversion.

Legacy SetObjectState, automatic PackageEffects and internal WorldFacts.switch all
check durability, jam, bar and locks. No implicit unlock/unbar or force check.
Evidence is captured after the bar update in the shared command transaction.
Checks cover query/parser, retry, bar/open separation, jam, legacy locks/navigation;
all focused tests must pass before W3 commit. No independent caches exist.


## W4 — independently identified ordinary locks

W3 commit 00a6db7 pushed (29 focused tests). LockSet declarations attach one or more
strict LockDefinition records to any openable. LockObject/UnlockObject explicitly
name the target lock; credential IDs are actual portable instances, never slugs.
Lock requires closed, unlock changes only the selected lock. PickLock unsupported:
no approved object/tool/activity rule chain in the pinned Adapter.

| Fields | Actual location | Class | Writer / consumer |
|---|---|---|---|
| lock_id, target_object_id | door_locks composite session identity | R | initialization / target validation |
| definition/policy, operable_sides, requires_closed | LockDefinition and fixed closed invariant | T | Package / handler |
| initial_locked/locked | LockDefinition / door_locks.locked | T/S | init, WorldFacts.set_door_lock / open, navigation |
| credential_binding | door_credentials(session,lock_id,instance_id) | R | init / credential admission |
| locks_for_object/effective_locked | authorized object view, any blocking lock | D | derived / query, open and passage |
| usable_credentials/can_unlock | query_world_action with actual credential | D | validators / permission-safe result, no credential list |
| broken/pick/replacement | deferred | conditional | no supported rules/lifecycle consumer |

DB 27→28 evolves existing lock/credential tables with stable lock IDs. Old IDs and
bindings are preserved as lock.<old object_id>; no new key instances are created.
Legacy LockDoor/UnlockDoor remain compatible for a unique lock and reject multi-lock
ambiguity. New commands use existing sync/async submission, controlled-NPC grants
and availability query. Existing NPC single-door intents remain; new lock-ID NPC
Evaluation conversion is not implemented. W5 must extend recursive key accessibility.

W1–W4 review: fixed missing operation registration/imports; guarded legacy switching
against destroyed/jammed/barred/locked states; rejected lock-without-openable and
legacy+new duplicate lock declarations. Shared SQLite remains sole authority.
Credential lists are absent from public views. No new transaction/cache/RNG layer.
Full pytest suite and migration compatibility are run before proceeding to W5.


W4 validation result: all 1,227 collected cases covered by four complete file
partitions (300, 395, 231, and 301). The last partition initially had one stale
schema-shape equality assertion; changed it to preserve every original column/row
and compare replay against the upgraded snapshot. That test then passed separately.
No runtime failure remains from this run. Ruff check/format and diff check pass.
Historical /25 nonempty save migration is tested with genuine old committed code
in another process. Full logs are checkout-local `.test-tmp/w4-final*.log`.
The isolated checkout now has its own locked Python 3.12 venv; no global PYTHONPATH
is used because it would incorrectly override historical runtime subprocesses.


## W5 — containers and single placement authority

W4 878d93b pushed. DB 28→29 preserves all old columns/rows and adds nullable
container_id while widening the existing objects location constraint to exactly one
of ground, direct carrier or parent container. Self/cycle constraints and controlled
ancestry checks guard the relation. No child absolute coordinates or second owner.

| Fields | Actual model/storage | Class | Writer / consumer |
|---|---|---|---|
| container, capacity weight/volume, nesting, accepts, opening | Container typed config | T | Package / scoped ContainerService.audit; see current correction below |
| item external volume, explicit fit and content group | WorldObjectDefinition | T conditional | Package / only enabled capacity/fit checks |
| placement kind/scene/cell/carrier/container | objects exclusive relation | S/R | WorldFacts.transfer_item / recursive placement |
| quantity/charges | existing objects.item_json | S | existing mechanical unit / preserved on whole-instance transfer |
| contents/ancestry/root carrier/effective location | reverse links and bounded WorldFacts.placement | D | derived / query, key, evidence and inventory |
| accessible/blockers | item_accessible plus parent open/lock/destroyed | D | derived / key validation and InventoryState.accessible |
| contents weight/capacity | recursive own-weight*quantity; external child volume once | D | audit / limit admission; unknown rejects required measurement |
| inventory_projection | existing mechanical entries + characters carried-slug | D materialized | CharacterFacts scoped rebuild in same transaction |

PutItem/TakeItem use the registered closed command/parser/query with sync/async
submission. Whole instances only; source may be owned, reachable ground or an
accessible container. Nested container→container paths supported. Cross-role handoff
uses existing drop/pickup and reprojects every descendant to the actual root carrier.
NPC typed commands require existing control/access grants; new container intents
are not wired into NPC Evaluation. Existing pickup/drop intents continue to work.

Capacity null means explicitly no such limit; unknown item measurements stay null
and fail any enabled corresponding limit. Fit groups are explicit scenario data,
not inferred from size. Current admission allows at most two container levels;
ordinary items do not count. The legacy nesting_limit 1..8 field is parsed for
Package compatibility, but no longer controls runtime depth.
Closed/locked ancestors prevent key use and set mechanical inventory accessible=false.
Pinned Engine item and equipment consumers check accessible; tool checks remain
unsupported in the pinned check evaluator. Equipment/casting/effect bindings reject
unsafe transfers, including descendants of a moved container. No encumbrance claim.

Destruction policy retains parent/contents identity and permits contents access
through the destroyed shell; the shell's operations remain disabled. No guessed
spillage locations. Queries hide closed contents and contents weight. Transfer
before/after evidence freezes each actual stage. Candidate audit is pure and
shared savepoint rollback includes facts, projection and evidence; a rejection
receipt is still committed by the existing command protocol.

Current correction after the Openable merge: candidate admission checks the moved
subtree plus old and new ancestors, not every Session container. Aggregation may
read their other contents. OpenableService.allows_contents / contents_accessible
now own shell and ancestor-chain access semantics, reused by WorldFacts,
ContainerService and object views. See [ContainerService progress and remaining
dependencies](CONTAINER_SERVICE_PROGRESS.md) for verified scope and tests.

Checks: 72 container/openable/multi-lock/legacy-item cases passed, including all
legacy transfer fault injections. Added cross-role nested ownership, synthetic
container destruction and independent-process nested restore. Original scenario
case fixtures use explicit World fit/capacity definitions attached to approved
mundane mechanical token instances; they do not claim official item capacities.
Partial stack transfer, arbitrary attachments and equipment instance rebinding
remain unsupported. New processes resolve the same IDs and fixed package binding.


## W6 — dynamic object footprints and navigation

W5 b48995e pushed (72 legacy/item/capability cases + 21 World/recovery cases).
SpatialObject supplies an explicit bounded relative footprint, fixed axes, mobility
policy and open/closed/destroyed blocking mapping. No size-derived geometry.
ObjectSpatialService projects the current objects.cell, state and durability into
the existing GridTopology. Legacy static blocked_cells are preserved verbatim.
Shared doors are projected only through both existing passage endpoints, with set
union avoiding duplicated obstacles. Carried spatial-footprint objects are rejected.

| Fields | Actual location | Class | Writer / consumer |
|---|---|---|---|
| grid/scale/static terrain | existing MechanicalGrid, cell_size_ft | T | existing Package / upstream GridTopology |
| footprint/state blocking/mobility | SpatialObject | T | Package / current projection and placement admission |
| anchor_cell | objects.cell | S | WorldFacts.place / footprint projection |
| occupied_cells/passability | relative offsets + current state and existing topology | D | ObjectSpatialService / navigation, step, passage, placement, views |
| spatial revision/cache | existing world_version; no retained cache | D | shared commit / rebuild every query |
| orientation/dimensions | deferred | conditional | fixed axes; no rotation or dimension consumer claimed |
| vision occlusion/cover candidates | explicit unsupported query fields | conditional | no dynamic sensory/RE obstacle adapter yet |

RepositionObject is an ordinary registered command requiring authenticated
TrustedContext.can_place_world_objects. The capability must explicitly permit
GM placement; fixed objects/shared passage doors cannot be repositioned. It checks
all footprint cells against bounds, actors, static/dynamic obstacles and passage
standing/threshold cells. Normal PlaceObject cannot bypass this capability policy.
No player lifting/pushing/strength-cost path is claimed. Sync/async and controlled
NPC command envelopes work with the trusted GM grant; no NPC intent conversion.

Destruction retains obstruction under disable_preserve_obstruction; the explicit
new disable_spatial_policy permits configured destroyed blocking. The internal HP
application audits the resulting spatial candidate before adoption. Open/close
submission runs the common final spatial invariant before commit. Combat dynamic
obstacle guards remain. Current movement projection is not fed into LoS/Cover.

Validation: 13 initial World spatial/container/open tests and 16 spatial/durability/
navigation tests passed. Includes old-plan execution blocked by new multi-cell
obstacle, cancellation/replan after clearing, denied GM permission, collision,
bounds, replay and evidence-write rollback. No DB migration is needed: anchors
and version already have authoritative storage. Recovery reconstructs projections.


## W7 — local ordinary lighting and immutable perception inputs

W6 c63c296 pushed. DB 29→30 adds object_lights(enabled,mode), initially empty for
old saves. LightEmitter has strict modes, normal/hooded ranges in feet and explicitly
uses dim_outer_radius_ft (distance from source, not an additional distance).
Only grid_radius_static_los, ordinary scenario source_kind, sunlight=false and
explicit scenario_unmetered fuel policy are admitted. These are original scenario
emitters, not infinitely fueled SRD torches/lamps or persistent spells. Finite fuel,
real-time timers, Effects sources, cones and sunlight emitters are unsupported.

| Fields | Actual model/storage | Class | Writer / consumer |
|---|---|---|---|
| ambient illumination | existing MechanicalGrid.default_lighting/lighting | T | Package / base environment, unchanged |
| profile/shape/ranges/modes/source/sunlight/fuel policy | LightEmitter + LightMode | T/R | Package / LightService |
| initial/enabled/mode | LightEmitter / object_lights | T/S | init, WorldFacts.set_light_state / projection |
| source_object_id/effective_origin | same object_id + WorldFacts.placement | R/D | actual relations / carry and container projection |
| container transmission | Container open/closed explicit transmit/block/unknown | T | Package / recursive shading |
| effective illumination/contributors/uncertainty | LightService.project | D | existing GridTopology distance/LoS/cover / query, snapshot, evidence |
| fuel balance/time/orientation | deferred | conditional | no new stock, clock or rotation without a consumer |

SetLightEnabled/SwitchLightMode are closed typed commands with query, parser,
authorization, sync/async and combat/activity guards. New NPC Evaluation conversion
is not implemented; explicitly controlled NPC commands remain authorized through
existing grants. Public get_illumination reads only this actor's public scene and
filters hidden source identities. Closed contents do not leak through contributors.

Light positions follow carrier/parent facts automatically. Contributions combine
by the greatest bright/dim/dark category, never arbitrary addition. Existing static
Grid LoS/Total Cover gate propagation. Dynamic movement-only objects/closed passage
rays have explicit uncertainty rather than assumed visual opacity/transparency;
unknown container transmission likewise produces illumination=None/status=unknown.
A light on independent spatial geometry is rejected until anchor/occlusion semantics
exist. Unknown rays are frozen as light_uncertain_cells; perception refuses to
adjudicate those cells as definite visibility. Mechanical snapshots reject uncertain
lighting instead of serializing guessed brightness. Active light sources retain a
combat initialization guard pending the real combat environment adapter.

EventCapture stores projected local grid plus stage sources at before/after light
updates and all later movement/container events. Historical reads use frozen JSON.
Perception consumes that grid; diagnostic source lists are stripped from its signal
environment. Noncombat rule request/recheck snapshots use the same projection.
No noticing/Knowledge/Memory facts are written by lighting. Destroyed sources cease
contributing while identity/history are retained (synthetic W2 application scope).

Verified rules: SRD 5.2.1 p.11 Vision and Light; engine GridTopology implements the
existing distance/LoS/light categories. Standard lantern fuel/Bonus Action semantics
are not imported into these scenario emitters. Tests verify carried/fixed emitters,
mode/shading/movement, before/after frozen evidence, real Perception admission,
mechanical snapshot input, uncertainty, fault rollback, replay and fresh-process state.


W7 review/validation: 85 Perception/evidence/spatial cases passed in the combined
86-case run; one dynamic-obstacle endpoint uncertainty case exposed a missing
endpoint check. Fixed it and reran all six light cases successfully, including
that regression and internal destroyed-emitter adoption. Ruff and format pass.
The synthetic destruction test proves contribution removal, not an object attack.


## W8 — one-shot mechanism and complete public scenario

W7 3bf1989 pushed after checking the same authorized origin and explicit attachment
push instruction. DB 30→31 adds object_mechanisms, empty for old saves. No new event
queue, command transaction layer or timer exists. Manual actor-adjacent triggers,
armed→spent and reset=none are supported; automatic pressure/weight triggers are not.

| Fields | Actual model/storage | Class | Writer / consumer |
|---|---|---|---|
| mechanism definition, trigger and conditions/reset | strict Mechanism config | T/R | Package / MechanismService |
| status initial/armed/spent | Mechanism.initial_status / object_mechanisms | T/S | init, WorldFacts.consume_mechanism / query, admission |
| ordered actions/target capability | MechanismAction tuple of set_open_state/set_light_enabled | T/R | Package validation / preflight, internal capability calls |
| source command/activation/adoption | existing trigger_history + source first event | R | shared mechanism commit / replay, unique one-shot adoption |
| invocation_origin | existing private _WorldEffectOrigin kind=MECHANISM | internal | admitted graph constructs / target+value qualification |
| can_activate/linked_result_preview | object view and same validators; targets hidden | D | derived / authorized query |
| region/multiple uses/disabled/reset clock | deferred | conditional | no automatic, multi-use or disabling consumer |

ActivateMechanism is registered in both closed operation unions, wire parser,
authorization, sync/async submission and availability query. The trusted command ID
travels internally; callers cannot submit an invocation origin. Result target/value
must belong to the admitted immutable graph. No boolean bypass or nested public
submission. Locks, jam, bar, HP and door occupancy are prechecked, then rechecked
against sequential candidate state. Target already at value is a result no-op;
a successful chain still consumes the one-shot mechanism. Any forbidden result,
missing target, evidence/adoption/storage failure rolls back the entire command.
Recursive/self/other-mechanism targeting, arbitrary scripts and damage results are
rejected during Package validation. NPC Evaluation conversion is not implemented;
explicit trusted control remains available. Scope/evidence follow each target scene,
not a broadcast to all NPCs. Trigger adoption reuses source event and command IDs.

### Original scenario

`examples/fixtures/world_station.yaml` and `examples/world_station.py` are standalone
runtime artifacts; the example does not import tests. The Prism Relay has shared
locked door, real distinct credential tokens, separately locked case, nested pouch,
portable bead/fixed ordinary scenario emitters, multi-cell workshop obstacle and
one-shot lever. Mechanical token slugs do not imply official container/light rules.

The example performs real pickup, put and take, confirms closed nested key denial,
confirms locked-target lever failure, saves/closes the first process, restores in a
new Python process, replays the original receipt, unlocks case/door, activates lever,
crosses the shared passage, records actual blocked movement, performs explicitly
trusted GM placement and navigates through the cleared route. The portable source
moves with the nested carrier chain. Result: workshop (1,0), world_version 32,
event_seq 49 in the demonstrated run, bright local illumination; no invented attack.
Its report separates real_player_paths, gm_management_paths and internal_synthetic_tests.

### W8 review fixes and proof boundaries

- Fixed the Perception evidence parser gap found in W7: new writes use /6 with strict
  light metadata validation; /4, /5 and the explicit W7 extended-/5 shape remain
  readable. Real public project_event and authorized Observation tests pass; source
  debug lists are removed before Host/JEV judgment. No retroactive evidence rebuild.
- Public carried-item view now hides inaccessible contained entries, in addition to
  hiding closed object contents/weights/source IDs. Trusted mechanical snapshots
  retain complete authoritative entries with accessible=false. No cognition fields
  were added to World. The example and nested-key tests verify the boundary.
- Strengthened spatial audit against legacy blockers and passage reserved cells;
  shared doors are limited to fixed single-threshold geometry until orientation
  semantics exist. Spatial destruction policy requires its actual capability.
- Strengthened internal settlement identity/version types and invalid read inputs;
  no player HP patch or user-provided trusted-origin flag was added.

The exact W4/W8 reviews, validation output and remaining integration paths are in
`docs/reviews/WORLD_W1_W8_REVIEW.md`. Final all-test partitions and static checks
are required before W8 commit/push. This is noncombat local capability delivery,
not full PHB/DMG/MM coverage or an authenticated HTTP/Host integration.

## Remaining original field catalog coverage

| Original direction | Handling in this implementation |
|---|---|
| WorldState/session/rules/time | existing Session binding and world_states.game_tick; abstract game ticks, no seconds/fuel conversion invented |
| Calendar/date/day/night/due time | deferred: no calendar consumer; existing Activity/Effects own their established due work |
| Scene definition/flags/visited/spatial | existing Package SceneDefinition and scene_instances; no actors re-created on revisit |
| actors/objects/exits projections | actual CharacterFacts positions, unique object parent chain and current shared passages |
| Terrain/barriers/grids | existing static grid untouched; W6 current movement footprints only; complex terrain/flight/fall left to actual RE paths |
| identity/material/size/weight/placement/durability | W1/W2/W5/W6 tables and mappings above; unknown measurements remain unknown |
| openable/locks/container/light/mechanism | W3/W4/W5/W7/W8 capabilities and actual consumers above |
| uninstantiated quantities/resource pools | deferred; existing object item units retain quantity/charges, no duplicate fuel balance |
| environment/weather/water/temperature/severity | deferred; only existing base illumination and required spatial inputs used |
| concealment/sensory/discovery | existing Signals/Scope/Evidence/Perception; no World discovered_by/password/memory fields |
| generic interactions | closed typed operations and shared validators; no script interpreter, arbitrary patch or cooldown table |
| interaction progress/cooldowns | deferred to actual Activity/Effects support; no empty object progress system |
| persistent magic/area/lifecycle | existing mechanical Effects remain authoritative; no new magic or World lifecycle |
| arbitrary attachment/vehicles/lair/planes/Bastion | deferred; no consumer required for these eight bounded batches |

## Integration boundaries and merge readiness

RE dependency/version unchanged. Needed future work: real object-target settlement
ABI and Adapter/RNG/resource chain for attacks/repair; Combat eligibility/cost/
reaction sequencing and dynamic environment snapshot; Activity/tool semantics for
picking/forcing; Effects for magical light/darkness/sunlight and lifecycles; explicit
vision-occlusion/Cover adapter for dynamic geometry; finite fuel with authoritative
tick/accounting; NPC Evaluation intent DTO/conversion for newly introduced commands.
Existing NPC open/close, unique-door lock and pickup/drop/navigation intents still
use the actual reliable handoff path and are covered by the existing suite.

No main merge is performed. Local delivery is reviewable subject to final tests and
these explicit supported-range limits. The original checkout's GC5 remains excluded;
its independent uncommitted migration 26 cannot be combined by copying this branch's
schema number. Integration must renumber and test both migration histories first.


Final field review clarification: `damage_threshold=None` means no applicable
threshold, while literal `"unknown"` preserves an unknown threshold. Container
capacity `None` explicitly omits that limit; `"unknown"` refuses adding contents
with unknown_container_capacity. Item measurements remain nullable unknown and
never become zero. Definition references cannot name conflicting templates, and
legacy/new lock declarations share one unique lock-ID namespace.


## Final delivery verification

Frozen-code full regression: **1,259 passed**, all four complete file partitions
exit 0: 314 / 396 / 242 / 307. Runtime: isolated locked Python 3.12.14 environment.
Final logs: `.test-tmp/world-final0.log` (113.07s), `world-final1.log` (96.57s),
`world-final2.log` (116.88s), `world-final3.log` (74.68s). Two existing upstream
Lark deprecation warnings per test process; no test skips or failures in the final
runs. Ruff check, Ruff format check (148 files), and git diff --check pass.

The first final pass found two facade-composition failures; both were fixed by
WorldHandler-owned service assembly and complete affected partitions passed.
After the final unknown-field regressions were added, **every partition was rerun
on the frozen implementation**, yielding the 1,259-case clean result above.

Remote main was read-only verified at 1fa7cea5dab7bb95f3f6fa975f8e681c691937a6
before W8 delivery. Only origin/feat/world-w1-w8 is pushed. The original checkout
remains at the original HEAD with its same GC5 dirty-file list; the delivered code
is in this isolated checkout and the task remote branch. No reset/stash/main merge.

Within the documented noncombat scope this branch is ready for merge review.
The open RE/Combat/Activity/Effects/Perception/NPC boundaries above remain explicit
limits, not completed integration. Combining GC5 first requires migration conflict
resolution and combined testing. No automatic merge is performed.


### Batch commits and push results

| Batch | Commit | Delivered capability | Push |
|---|---|---|---|
| W1 | 5d7a5d3 | fixed object definitions and authorized views | confirmed origin/feat/world-w1-w8 |
| W2 | d6cdfe2 | object HP and private admitted settlement application | confirmed |
| W3 | 00a6db7 | shared open state, independent jam/bar and sides | confirmed |
| W4 | 878d93b | stable multi-lock instances and genuine-save migration | confirmed |
| W5 | b48995e + current ContainerService correction | unique placement; scoped admission; two container levels; shared Openable access | see CONTAINER_SERVICE_PROGRESS.md |
| W6 | c63c296 | dynamic multi-cell movement geometry and GM placement | confirmed |
| W7 | 3bf1989 | ordinary local emitters, snapshot and frozen stage evidence | confirmed |
| W8 | c2e8b7b | atomic one-shot mechanisms, complete public scenario and final review fixes | confirmed |

A following documentation-only delivery commit records this table. The W8 push
returned c2e8b7b on the authorized task branch; no main change or PR merge occurred.


## Follow-up status (2026-10-17)

The historical W1-W8 evidence above is unchanged. The Openable extraction and
bounded noncombat picking/forcing increment is tracked in
[OPENABLE_SERVICE_PROGRESS.md](OPENABLE_SERVICE_PROGRESS.md). Its acceptance table
supersedes historical picking/forcing TODO descriptions only within its explicitly
verified range. Combat, magic, real object attacks and missing lifecycle/projection
contracts remain OPEN; the new service does not close those dependencies.


## Mechanism follow-up — 2026-10-18

The historical statements above retain their original scope. MECH-01 is now
verified complete on `feat/world-mechanism-service`: local typed candidate maps
advance in action order, Openable retains each qualification check, and OSS checks
the given complete candidate scene. Both legal free-then-occupy and real-conflict
cases are tested through Session query/submission without query writes. The bounded
repeatable/lifecycle/automatic extension and its OPEN dependencies are documented
in [MechanismService contract](MECHANISM_SERVICE.md) and
[progress](MECHANISM_SERVICE_PROGRESS.md). W-02, SA-01 and other component work
are not closed. No main merge is implied.

## GridService follow-up — 2026-10-18

The historical W1–W8 implementation/API record above retains its original scope.
On `feat/world-grid-service`, based on `main@0554add`, SM now extracts grid reads,
current-space composition and geometry queries from WorldHandler while preserving
the existing exploration, interaction and authorized navigation paths. Character
and WorldObject keep their respective position authority. Occupancy, movement,
difficult terrain, vision and cover remain distinct; no exploration combat budget
or time payment is added. See [GridService](GRID_SERVICE.md) and
[progress/validation](GRID_SERVICE_PROGRESS.md).

This is only a W-01 slice. RE/Combat dynamic-space consumption, Character multi-cell
space and future Activity/Effect adapters remain OPEN in their existing module
records. No future-contract integration test is reported as passed, and no main
merge is implied.


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
