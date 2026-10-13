# GC-4: shared passage locks and owned credentials

Baseline: clean `1098bd749eaa2ca114f8d7e36e8058f6bec0089f` on
`feat/sm-gc4-lockable-passages`, equal to fetched origin/main and containing
GC3 `80bc1af59993d29f3281e99b06de60915dc450a9`. No applicable AGENTS.md.

## Responsibility and support scope (fixed before implementation)

| Fact / action | Owner / boundary |
|---|---|
| Shared door open/closed | Existing WorldFacts.objects, one door_object_id |
| Independent locked state and approved instance bindings | WorldFacts, additive Schema25 tables |
| Actual position and complete owned inventory consistency | Existing CharacterFacts / GC3 facts |
| Lock/unlock admission and coordination | WorldHandler, existing local endpoint and actor guards |
| Command authorization, fingerprint, OCC and receipt | Existing SessionAccess / CommandRuntime |
| Frozen actual-stage evidence, events and Outbox | Existing EventCapture / EventPersistence |
| Navigation plan and blocked/resume/cancel | Existing GC2, static planning unchanged |

Only explicit public deterministic noncombat mechanisms and actual GC3 portable
instances are supported. Lock/unlock requires a configured credential currently
carried by the actual actor, including for a Host-controlled actor. There is no
lockpick, breakage, repair, container, equipment, consumption, spell, time, RNG,
automatic credential search, remote unlocking or LLM extension.

Open/closed remains separate from locked. Valid states are closed/unlocked,
closed/locked and open/unlocked. Unlock never opens; open never traverses; close
never locks. Both local endpoints allow interaction independent of exit direction.

The design reference is [Evennia SimpleDoor](https://www.evennia.com/docs/latest/Contribs/Contrib-Simpledoor.html):
two-sided door controls can be extended with locks. Its superuser bypass and
runtime dependencies are not adopted. This is not complete D&D lock rules.

## Implemented API and trusted content

Package `state-machine-foundation/5` retains /4 navigation and portable item
configuration and requires a closed `lockable_doors` list (maximum 64):

```json
{
  "object_id": "object.platform_gate",
  "initial_locked": true,
  "credential_instance_ids": ["object.amulet_a"],
  "policy": "public_owned_credential"
}
```

Each target is an existing fixed shared passage door with two public fine-grid
ports; credentials are a nonempty, unique, bounded list of existing /4 portable
instance IDs. Duplicate targets, foreign references, type slugs, additional fields,
other policies and open/locked initial states reject. A single instance may bind
multiple doors only through explicit declarations. Old Package /1–/4 digests,
grants and ordinary switch behavior remain unchanged.

The example uses actual pinned `amulet` assets as specific credentials accepted
by an original public mechanism. Matching is by instance ID, never slug, name,
spellcasting focus, equipment or magic effect. The canonical `key` asset is a
magic card and is not used as an ordinary key. Ordinary standalone key content
is future work; no asset loader or SRD content is changed.

| TypedCommand type | Typed payload (`door-lock/1`) | Effect |
|---|---|---|
| `world.unlock_door` | `UnlockDoor(scene_id, object_id, credential_instance_id, expected_cell)` | Clear locked only |
| `world.lock_door` | `LockDoor(scene_id, object_id, credential_instance_id, expected_cell)` | Set locked only; door must already be closed |

The existing envelope supplies authenticated actual Actor, Session, command key
and expected world version. Closed data adaptation rejects extra caller fields,
including trust/bypass flags. Both actions require the actual Actor's current
local endpoint, standing point, visibility, clear adjacent reach, control, state,
activity and noncombat qualification. The PC's Scene and door's storage Scene do
not constrain another Actor's valid local port. Traversal direction never grants
or removes the reverse port's control capability.

Before any no-op, the executor validates current ownership, approved binding and
the complete GC3/RF4 inventory closure. The credential must be one accessible
unit currently carried by this Actor in this Session. Unknown, foreign, ground
and mismatched credentials share `credential_unavailable`; the executor never
returns another carrier or the accepted binding list. Quantity, charges, owner,
equipment, resources and casting state do not change. A valid target-state no-op
commits its ordinary receipt with no lock event/version change. Dropping the
credential after unlocking does not relock; the next lock action checks afresh.

`get_spatial_view` adds `SpatialDoor.locked`: boolean for configured locks, None
for ordinary doors. Only currently visible local public ports are returned; no
credential bindings are added. Ordinary visible ground item IDs retain their GC3
visibility. `query_world_action` reuses execution admission and returns only its
existing bounded availability/reason/version. Both queries reuse current control
authorization, write no facts and create no Observation.

## Persistence, guarded consumers and provenance

Schema25 adds `door_locks(session_id, object_id, locked)` and
`door_credentials(session_id, object_id, instance_id)`, with composite identity,
uniqueness and same-Session foreign keys. Only WorldFacts writes them. Door
open/closed remains in the original `objects` row, not in the lock table. SQL
triggers also refuse open/locked or nonswitch lock facts. Domain readers reject
contradictory current state and missing configured lock capability; they never
repair it. Existing migration SQL is unchanged and legacy doors acquire no rows.
Initialization follows /4 item initialization only during fresh Session creation.
Restore and revisit never reapply initial lock or item configuration.

| Consumer | Guard / result |
|---|---|
| Manual SetObjectState and legacy helper | Local target authorization then lock guard before no-op/write |
| WorldFacts.switch | Central domain lock guard; direct controlled ordinary switch cannot open a locked door |
| NPCDoorIntent | Existing pending handoff dispatch reaches the same ordinary switch guard |
| StepActor / EnterScene / MoveActor / TraversePassage | Shared movement projection treats closed locked door thresholds as blocking |
| GC2 advance | Ordinary opening refuses at the door; activity becomes blocked with no false physical event |
| GC2 planner | Existing granted static topology only; no remote live lock read or credential search |
| Package effects | Existing typed automatic source reaches the same switch guard; whole cascade rolls back |
| CheckWorldEffects | Same automatic source boundary; new lock refusal rolls back every consequence and RNG and retains a refusal receipt |
| Spatial/public reads | Local authorized visible lock state only; no accepted credential catalogue |
| Combat admission | Existing closed-door integration guard and noncombat-only world actions retained |

The real Check settlement adds only `door_locked` and `invalid_lock_state` to its
existing savepoint-refusal allowlist; other error/storage semantics are unchanged.
There is no new automatic unlock effect, remote privilege or default PC actor.
Locking does not add sensory geometry, opacity, cover or sound blocking.

`NPCUnlockIntent` / `NPCLockIntent` add only new output discriminators
`intent_type=door_unlock/door_lock`. Old output JSON and fingerprints remain
unchanged. Task/Result selection writes pending output without operating a door;
dispatch rechecks current controller, envelope identity, versions, position and
credential. Door-lock fields cannot be mistaken for NPCDoorIntent or item transfer.

One existing short transaction covers lock mutation, actual-stage capture,
semantic `door.lock_changed`, event identities, queue/Outbox, world version and
receipt. Domain components never commit or perform external I/O. The single
physical event uses phase `world.after_door_lock_update` and the actual Actor's
current Scene/Cell, including the reverse port. Audit payload may include the
credential ID; generic visual Signal text says only that someone operates a door
lock. Rejections create no bad-lock sensory fact or new cognition/Memory/Goal.
Old Evidence remains immutable and RF4 current-position capture is retained.

## Reproduction and support matrix

```powershell
.venv/Scripts/python.exe examples/lockable_passages.py
```

The [original Package](../examples/gc4_locks.json) and [public example](../examples/lockable_passages.py)
use the pinned assets and fresh real SQLite. The common navigation pump executes
all routes. Explicit authored decisions cancel the blocked attempt, navigate to
the amulet, pick it up, return, unlock without opening, navigate through ordinary
opening/traversal, then close and lock from the other side. A new Python process
restores and compares the public door, location, inventory and version views.
This demonstrates a controller-directed workflow, not LLM autonomous planning.

| Supported boundary | Executed evidence |
|---|---|
| Two doors, same-slug distinct amulets, shared instance across explicitly bound doors | Independent states, wrong twin/other instance refusal, PC/NPC and both ports |
| Local authorization | Ground, foreign ownership, wrong Scene/Cell, far, wall, unknown target, cross-Session Actor, revoked control |
| Qualification | Active navigation, authored NPC activity, zero HP, incapacitated metadata and actual initialized combat reject |
| State transitions | Unlock does not open; close does not lock; open cannot lock; fresh no-op revalidates owned credential |
| Occupancy / direction | Real Actor at opposite threshold blocks close; one-way route still permits reverse-side lock actions |
| Current item relation | Public drop/acquire immediately affects next lock; no credential consumption or automatic relock |
| Navigation | Far unseen second-lock state changes neither plan nor prior actions; blocked Actor can receive credential and explicitly resume |
| Automatic origins | Locked Package and real Check consequences roll back atomically; unlocked automatic opening retains Package provenance |
| Reliable NPC | Real Task with explicitly typed synthetic Result; pending only, type conflict, dispatch location/drop changes, controller revocation |
| Reliability | Same-key replay/conflict, concurrent lock/open OCC, capture/SQL/evidence/Outbox/version/receipt faults, lost reply and restore |
| Historical compatibility | Genuine archived Schema24, atomic failure/retry, all original rows, old command receipt and old NPC JSON preserved |

Validation counts and independent review: [GC4 Review](reviews/GC4_LOCKABLE_PASSAGES_REVIEW.md).
No full D&D lock rules, lockpicking, destructive access, broken-lock repair,
container/equipment expansion, spell opening, inside-bolt policy, continuous
passage permission, automatic key search or next batch is included.
