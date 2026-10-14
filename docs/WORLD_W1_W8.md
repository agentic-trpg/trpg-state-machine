# World W1–W8 implementation record

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
| container, capacity weight/volume, nesting, accepts, opening | Container typed config | T | Package / ContainerService.audit |
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
not inferred from size. Nesting 1..8 is an engineering bound, not a D&D rule.
Closed/locked ancestors prevent key use and set mechanical inventory accessible=false.
Pinned Engine item and equipment consumers check accessible; tool checks remain
unsupported in the pinned check evaluator. Equipment/casting/effect bindings reject
unsafe transfers, including descendants of a moved container. No encumbrance claim.

Destruction policy retains parent/contents identity and permits contents access
through the destroyed shell; the shell's operations remain disabled. No guessed
spillage locations. Queries hide closed contents and contents weight. Transfer
before/after evidence freezes each actual stage. Candidate audit is pure and
shared savepoint rollback includes facts, projection, evidence and receipts.

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
