# MechanismService contract (2026-10-18)

Implementation baseline: SM main `9ce60cbe4fdbf91dce6ffef811969482369fb08a`.
Architecture authority: `agentic-trpg/docs/MODULE_CONTRACTS.md`, verified main
`98e8d36b03bdc6b9578082bcdae1ed99ccbc5997`; v0.4 cognition decisions remain binding.
This document describes the bounded implementation, not completion of all traps.

## Ownership and closed operations

Mechanism is a WorldObject capability composed with Openable, Container, Light,
Durability and the existing portable instance path. WorldObjectService composes
MechanismService. WorldFacts owns `object_mechanisms` and activation adoptions;
there is no second HP, location, inventory, clock or ongoing-effect state.
Openable owns opening/locks/bars/jams; Light owns emitters; OSS owns geometry.
RE computes checks. Character/Effects/Combat retain their existing responsibilities.

New immutable declarations require Package `state-machine-foundation/10` and
`mechanism/2`. Package /1-/8 and original one-shot definitions remain accepted.
Package /9 is reserved for the original worktree's uncommitted Environment work;
this isolated main-based branch does not implement or accept that Package shape.
Unknown fields, scripts, arbitrary property writes and unsupported consequences
are rejected. For mechanism/1-/2 the closed consequence set is `set_open_state` and
`set_light_enabled`, at most eight steps per operation, sixteen operations,
sixteen trigger selectors, sixteen methods and sixteen destruction components.
A /2 target may itself have Mechanism plus the required capability; this never
admits an arbitrary activate-mechanism consequence. Original /1 graph restrictions
remain compatible.

Public commands use the existing envelope, permissions, expected world version,
fingerprint, receipt, shared SQLite savepoint and outbox:

| Command | Payload | Meaning |
|---|---|---|
| world.activate_mechanism | ActivateMechanism(scene_id, object_id, expected_cell, operation_id='activate') | Select a declared operation; legacy default preserved |
| world.service_mechanism | ServiceMechanism(scene_id, object_id, expected_cell, method_id, tool_instance_id=None) | Deterministic declared lifecycle method |
| world.check_mechanism | CheckMechanism(same fields) | Configured ordinary RE ability/skill check |

No caller can provide a DC, result, invocation origin, activation identity or
consequence graph. Read-only `query_world_action` uses the same admission and
ordered preflight as submission. `get_object` exposes runtime state to authorized
readers; consequences remain hidden. Missing capability, inaccessible object,
stale pose, exhaustion, blocking, permanent disarm and destroyed components refuse
explicitly. Same-state public object operations create no operation trigger.

## Definitions

A complete example capability (targets are declared WorldObject instance IDs):

```json
{
  "kind": "mechanism", "schema_version": "mechanism/2",
  "initial_status": "armed", "max_uses": null,
  "reset_policy": "none", "reset_delay_seconds": null,
  "frequency": "unlimited", "placement_policy": "accessible",
  "components": [], "destruction_policy": "disable",
  "operations": [
    {"operation_id": "up", "consume_on_noop": false, "actions": [
      {"kind": "set_open_state", "target_object_id": "object.gate", "value": "open"}
    ]},
    {"operation_id": "down", "consume_on_noop": true, "actions": [
      {"kind": "set_open_state", "target_object_id": "object.gate", "value": "closed"}
    ]}
  ],
  "triggers": [],
  "methods": [
    {"method_id": "release", "operation": "disarm", "required_tool": null,
     "ability": "dex", "skill": "sleight_of_hand", "dc": 15,
     "trigger_on_failure": "up"}
  ]
}
```

`max_uses=null` is unlimited; a positive bound counts activations, independently
of reset policy. Reset never refills finite uses. `reset_policy=none` allows
successive /2 activations without resetting; `manual` makes each activation spent
until a configured reset method; `after_seconds` stores a bounded deadline using
WorldTimeService and requires world-time/1. Legacy abstract ticks refuse second
conversion. A clock command crossing the deadline resets in the same transaction;
no wall clock, polling or synthetic Turn-to-seconds conversion is used.

`consume_on_noop=false` skips activation/state/history/events when every step is
already at its declared value. Any actual intermediate change counts as an
activation even when the final value returns to its initial value. A true policy
consumes one activation even when all consequences are no-ops. Legacy /1 always
consumes once. Exhausted instances cannot reset/prepare into another activation.

## Runtime and lifecycle

| State | Meaning |
|---|---|
| armed | Prepared and eligible, subject to position/access/components/frequency |
| unprepared | Stowed or not prepared; configured prepare method needed |
| spent | Waiting for configured reset, or exhausted; remaining_uses distinguishes them |
| blocked | Temporarily blocked; resume_status retains armed/spent/unprepared |
| disarmed | Permanent disarm; reset/prepare cannot revive it |
| broken | A configured component or the instance is destroyed; no repair path invented |

`remaining_uses`, `activation_count`, optional `reset_at_tick`, and optional
`resume_status` are the only additional runtime fields. Database CHECK constraints
prevent armed/exhausted and contradictory block/deadline combinations. Trigger
history uses activation adoptions, not duplicate actor/position/current-effect
facts. Components require actual Durability declarations and initialized facts;
zero initial component HP disables the instance at initialization. An admitted
ObjectDurability settlement updates mechanism lifecycle in the same transaction;
settlement replay creates no duplicate consequences. This is tested with the
existing internal settlement fixture, not claimed as a new player RE damage API.

Methods select disarm/block/unblock/prepare/reset. Deterministic methods may require
one explicit accessible owned tool instance with the configured item slug. Tool
possession is not tool proficiency. Check methods currently support ordinary RE
ability/skill checks without a tool; tool checks return
`unsupported_mechanism_rule`. Failure triggers are explicitly selected by the
method and must be eligible/preflightable. A legal failed check commits its actual
CheckRolled/RNG and optional configured activation. Illegal requests, stale
versions, evaluation faults and storage faults never become a character failure.
RE runs outside SQLite transactions; commit rechecks snapshot, version, RNG and
method dependencies. No player/model wait occurs under the write lock.

## Ordered consequences and identities (MECH-01)

MechanismService evolves a local mapping of typed ObjectSpatialState candidates
in declaration order. Each step calls Openable's transition validation with that
mapping; OSS checks the proposed complete scene against earlier candidates. Locks,
bars, directional jams, HP, threshold occupancy, grid/walls and collisions remain
checked at each step. Queries never simulate by writing or rolling back facts.
Actual execution uses the same capabilities sequentially inside the savepoint.
Opening A to free a cell before closing B is available and commits; a final real
collision or an intermediate invalid step refuses the entire operation.

Instance ID is the existing object_id. Activation ID hashes instance, source
command, source event sequence and operation. Consequence ID appends its declared
index. Internal capability origin admission binds the exact active consequence;
merely copying a declaration or constructing an origin does not authorize it.
New activation rows retain source command/event, actor and operation. Their unique
key, state/uses, consequences, receipts and outbox commit together. Both open and
light consequences include activation/consequence provenance. Legacy /1 also
retains `trigger_history['mechanism.' + object_id]` and its original command
fingerprint. New /2 activations never reuse that one-shot key. Reset does not erase
activation identity or past uses.

## Trusted automatic hooks

Automatic routing reuses PackageEffects' synchronous bounded queue, stable event
order and total limit of 32 records/consequences per command. Limit overflow or a
supported consequence failure rolls back the source operation, RNG (if any), all
mechanism changes, adoptions and events. No second generic trigger executor exists.
Definition indices are cached by Package digest and addressed by scene/target;
there is no per-frame Session scan. Matching mechanisms run in object_id order,
then trigger declaration order. At most one selector wins per instance/source
event. Activation adoption deduplicates repeated source delivery.

Each selector has exactly `kind, scene_id, operation_id, cells,
target_object_id, event_type, succeeded`, plus optional `value` (default null):

| kind | Selector and actual producer |
|---|---|
| enter_region | Nonempty canonical grid cells; actor.stepped/actor.moved/scene.entered from actual movement, passage or Activity navigation |
| object_operation | Explicit target and event_type: object.state_changed, object.light_changed, door.lock_changed, object.bar_changed, object.jam_cleared, item.picked_up, item.dropped, item.container_transferred |
| check_result | Explicit object target and succeeded Boolean; only the real adopted Openable/Mechanism check result, never an unbound generic check or client success flag |

A non-null value selects open/closed or enabled/locked/barred Boolean; null selects
any actual change of that operation type. Unused fields must be null/empty. Regions are absolute scene cells; moving within
one region is dwell, not entry. Crossing from another scene into it is entry.
The mechanism's actual root location must be in the selector scene; region sensor
location and consequence targets are explicit, independently declared. Existing
capabilities determine consequence scope, and uninitialized targets refuse.
`once_per_actor` means once per instance lifetime for that actor across all manual
and automatic operations; resetting does not clear it. `unlimited` imposes no
additional frequency bound. There is no represented per-Turn frequency yet.

Light before/after perception records share one operation: routing only consumes
the trusted transaction-local after stage. Container transfers only consume the
after record. Movement from/to facts and stage metadata are transaction-local typed routing
data; existing public movement payloads remain compatible. Routing does
not inspect Signals, OB, JEV or EVA and creates no additional cognitive calls.
Noncombat events retain existing perception behavior. Active-combat automatic
routing and public world mechanism commands remain closed. v0.4 participant Turn
Episode / external Round Episode routing remains Combat/Perception work; this
branch does not add per-action combat perception or close SA-01.

## Portable instances

Portable definitions retain the same object_id and World/Container/Character
inventory projections. No Item subsystem rewrite or new ownership store occurs.
`ground_prepared` requires an actual ground anchor to activate/prepare/reset;
picking up or packing an armed instance makes it unprepared, not disarmed.
Blocking retains its return state; a due reset while stowed returns to unprepared.
`accessible` explicitly permits owned carried/accessible contained operation and
triggering; all enclosing access checks remain active. Putting an accessible
mechanism in a bag never automatically disarms it. Permanent disarm survives
recovery into inventory. Existing rigid/spatial portability restrictions remain;
linked opening of a carried/contained target is explicitly unsupported.

## Storage compatibility and remaining contracts

Schema33→34 expands the original mechanism table, preserving every original
column value and row. Old armed becomes remaining_uses=1/count=0; old spent becomes
remaining_uses=0/count=1. New mechanism_activations starts empty for old saves;
old trigger_history and receipts remain untouched. Migration is transactional.
Revisits/reopens never reinitialize existing mechanism state.

The integrated Environment branch uses Schema35 for the ENV table and retains
main's mechanism migration34 unchanged. Openers distinguish main34 from the
historical ENV34 by actual table layouts, keys and activation indexes under the
existing migration write lock. ENV34 keeps its environment rows and runs the
missing main34 mechanism upgrade before ENV35 reuses the validated ENV table.
Main34 preserves its complete mechanism state/adoptions and adds an empty ENV
table. Unknown or partial layouts are refused without writes; all DDL/data/version
changes roll back together on failure. See [Environment storage compatibility](ENVIRONMENT_SERVICE.md).

| Unsupported capability | Required contract / owner |
|---|---|
| Damage, saves, conditions, ongoing effects | Trusted world/system-source typed rules.effect request, result adoption and Effects lifecycle; RE adapter + Character/Effects |
| Per-Turn/per-actor-per-Turn limits and Turn reset | Stable combat/Turn identities, timing and approved internal mechanical callback; Combat/RE |
| Public combat operation, action costs and Reaction | Combat command admission and resource Delta; SA-01 remains separate |
| Tool proficiency checks, material consumption, timed servicing | RE tool/resource payload + item quantity adoption and generic Activity scheduling; RE/World/Character/Activity |
| Arbitrary rule-result triggers, pressure/weight or dwell | Typed trusted fact producers and scoped selectors; corresponding domain owner |
| Repair or complex destruction behavior | Admitted repair/destruction policies; Durability/RE |
| Carried/contained linked opening and unsupported rigid poses | Explicit capability/space contract; Openable/OSS/Item |

These are explicit refusals or absent command/configuration variants, not empty
implementations. No other component's pending item is closed by this work.

## Mechanism effects contract increment — 2026-10-18

mechanism/3 adds a declared effect catalog and ordered rule_effect actions with
typed attack/save/damage/status parameters and single/area target selectors.
The separate SM `mechanism-effect/1` port retains actual mechanism source,
original command correlation, complete candidate snapshot/binding/RNG and bounded
owner validation. See [the effect contract](MECHANISM_EFFECTS_CONTRACT.md) for
types, ordering, four-state result mapping, fixtures and remaining capabilities.
Fixed RE /23 has no world-source rules.effect request. Production returns
unsupported; accepted damage adoption is fixture-verified and bounded.
Status/lifecycle, zero-HP/death packets, real effect integration, remote evaluation
and combat Episodes remain OPEN. Existing world linkage and lifecycle retain
their owners; no mechanism damage formula or status timer was added.
