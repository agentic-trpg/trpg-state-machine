# GC-2: durable noncombat navigation

Baseline: clean `9ea750bab2c1ff6d990a02b13b10ffff1c81ed88`, including RF-1
`203b704`. This is a bounded SM implementation, not a frozen cross-repository ABI.

| Data / execution | Owner |
|---|---|
| Actual actor position, HP and resources | existing CharacterFacts |
| Maps, objects and one shared door state | existing WorldFacts / WorldHandler |
| Validated shared static regions and actor/template grants | immutable Package /3 |
| Target, static execution plan, cursor, activity version and bounded result | ActivityRecords |
| Start, one-step advance, explicit resume and cancellation | ActivityHandler |
| World version, command identity/receipt and atomic commit | existing CommandRuntime / SessionAccess / shared Store |
| Physical action signals and stage evidence | existing WorldHandler / EventCapture |

Plans use only granted authored static grids and explicitly listed directed
passages. Local legs reuse pinned RE GridTopology.shortest_path; cross-Scene
composition combines endpoint legs deterministically. Remote dynamic objects,
actors and door state never enter planning. Actual execution uses WorldHandler.

Each command shares one outer transaction with world changes, progress, frozen
events, Outbox and receipt. Every advance performs at most one world primitive
or one lifecycle transition. A blocked activity releases execution eligibility;
only an explicit resume with current activity/world versions can retry its next
uncommitted step. A changed position requires a new authorized plan.

Navigation reserves NPC current_activity at lifecycle boundaries. Schedules
cannot start while navigation controls the actor; elapsed entries can still be
marked missed by explicit game-time synchronization. Existing scheduled or
authored activities prevent navigation start/resume. No per-cell cognition
re-evaluation, Memory/Belief/Goal/Plan update or automatic time advance is added.

Lifecycle-only events have no sensory signals. A program refusal is an activity
result, never an invented Observation. Physical movement and ordinary door
interaction retain actual-stage evidence and existing perception delivery.

The design borrows explicit start/advance/finish/cancel, interruption and
serialization from [CDDA Activities](https://docs.cataclysmdda.org/PLAYER_ACTIVITY.html).
It does not copy CDDA movement, time rules, backlog or engine implementation.

## Closed local API

All writes use `submit_command(TypedCommand(...), package, context=TrustedContext(...))`.
The envelope supplies Session, actual Actor, unique command ID and expected
world version. Context capabilities come from the authenticated adapter; JSON
cannot supply execution qualification, familiarity or a trusted source.

| Command type | Payload (`navigation-activity/1`) | Resulting behavior |
|---|---|---|
| `activity.start_navigation` | `StartNavigation(activity_id, target_scene_id, target_cell)` | Validate and persist a plan; no movement and no claim of arrival |
| `activity.advance_navigation` | `AdvanceNavigation(activity_id, expected_activity_version)` | One validated StepActor, ordinary door open, TraversePassage, or lifecycle transition |
| `activity.resume_navigation` | `ResumeNavigation(activity_id, expected_activity_version)` | Explicitly reclaim a blocked plan at its exact expected position |
| `activity.cancel_navigation` | `CancelNavigation(activity_id, expected_activity_version)` | Release execution eligibility and retain immutable history |

Activity IDs match `activity.[a-zA-Z0-9_.-]{1,119}`. Target references are bounded
strings validated against granted Scenes and canonical, walkable Cells.
Activity versions are positive integers (booleans rejected). The data adapter
rejects extra fields and arbitrary operation names. No scripts, SQL, function
names, implicit moving-person targets or executable Plan text are accepted.

`read_navigation_activity(session_id, actor_id, activity_id, package, *, context)`
returns a detached `NavigationActivity`: target, status, activity version, cursor,
static plan/length, last bounded result, source/last command IDs and current
world version. A trusted Session owner may read their PC or a currently
controlled NPC; `NPCContext` may read only its own controller-authorized actor.
Reads neither advance, acknowledge nor change the world. The plan contains only
granted static coordinates/connections; blocked results omit obstacle identities
and hidden dynamic information. This is a local SM interface, not an ABI freeze.

## Knowledge, planning and execution

Package `state-machine-foundation/3` adds a required closed `navigation` object:

```json
{
  "regions": [
    {"region_id": "region.public", "scene_ids": ["scene.platform", "scene.workshop"],
     "passage_ids": ["passage.station"]}
  ],
  "actor_grants": [{"actor_id": "npc.caretaker", "region_ids": ["region.public"]}],
  "pc_template_grants": [{"pc_template_id": "pc_template.visitor", "region_ids": ["region.public"]}]
}
```

Use actual template IDs from the approved Package. At most 32 shared regions,
1..32 public fine Scenes and 0..64 explicit passages per region are admitted;
all references, duplicates, grants and passage endpoints are checked. A region's
passages must have both endpoints inside that region. Grant unions still never
implicitly add an unlisted secret connection. Package /1 and /2 load without
navigation knowledge and retain their original raw-data digest computation.

The planner uses the actual actor's current canonical position, the selected PC
template grant or NPC actor grant, authored grids, and explicit directed exits.
RE supplies all local shortest paths. Endpoint composition picks a deterministic
bounded route of at most 4096 movement/passage legs. Overlong candidates are
pruned while shorter pending candidates remain eligible. Missing knowledge or
no approved route is a capability result, not proof that the physical world has
no road. Shared region definitions are not copied into every NPC's state.

Dynamic SQL positions, objects and door state do not enter route choice. At
execution, WorldHandler rebuilds its existing temporary movement projection and
checks actual actor readiness, position, public scope, visibility, obstruction,
occupancy and directed passage authorization. Shared doors use the actor's local
endpoint regardless of their storage Scene. A closed ordinary passage door is
opened as one primitive; the cursor stays on the passage until a later advance
actually traverses it. Other doors, movable objects and blockers are never
removed, moved or cleared. Ordinary manual commands remain subject to busy
checks while navigation owns the actor.

## Progress, versions and retry

| Status | Execution eligibility | Continuation |
|---|---|---|
| `active` | One activity per Actor | Advance with current versions |
| `blocked` | Released | Explicit Resume after an external decision/change |
| `completed` | Released | Historical only; new destination requires a new ID |
| `cancelled` | Released | Historical only; old handles cannot restart |

Start creates version 1/cursor 0 even at the destination; a subsequent advance
performs completion without invented movement. Successful primitives and
lifecycle changes increment `activity_version`; opening a door increments the
version without incrementing the route cursor. Results distinguish `started`,
`progressed` (step/passage/open_door), `resumed`, `arrived`, `blocked`,
`capability_unavailable` and `cancelled`. There is no automatic replan or resume.

The command's `expected_world_version` applies normal Session OCC, while
`expected_activity_version` protects this plan/cursor. Unrelated committed world
changes require a refreshed world version but do not reset progress, rebuild the
plan or retry a blocked activity. Same command key/arguments return the original
receipt; changed arguments conflict. Stale activity versions reject. A current
version cancellation of an already cancelled record is a no-op; a completed
record cannot be cancelled or restarted. Historical activity IDs cannot be reused.

`pump_navigation(submit, read, get_receipt, session_id, actor_id, activity_id,
package, *, context, request_id, budget=32)` is a convenience over these same
public operations. Its 1..64 slots each use a deterministic existing command ID
and an independent transaction. Reuse **the same request_id and budget** after an
interruption; committed slots are read from receipts before any new submission.
Use a new request ID to authorize another batch. There is no separate persisted
pump task platform or receipt store. A lost reply is reconciled with the original
receipt, never by blindly allocating another command ID. The driver stops on its
budget, blocked/terminal status, rejection or OCC conflict. It does not call an
LLM, sleep, reserve action resources or advance Game Time.

## Qualification and shared commit

ActivityHandler has explicit Store, ActivityRecords, CharacterFacts, WorldHandler
and EventCapture dependencies. Its records store an execution plan, not current
position, HP, topology or RNG. `service.StateMachine` only composes/delegates;
activity components and the driver do not depend on it. ActivityRecords alone
writes `navigation_activities` and owns the bounded NPC navigation marker at
claim/release boundaries. CharacterFacts/WorldFacts retain actual fact writes.

For each primitive ActivityHandler issues a private scope bound to the owning
records issuer, current UnitOfWork, Session, actual Actor, active activity/version
and exact primitive. WorldHandler revalidates that persisted qualification. A
caller-provided activity ID, a copied token from another issuer or an expired
transaction cannot bypass `current_activity`. Qualification only relaxes the
matching navigation busy marker; other actor state/authorization checks remain.
It never combines with Package/GM automatic-world-effect provenance.

PC operations use existing PC authorization. NPC start requires both the Host's
current control grant and a persisted NPC controller. Advance/resume also require
the original controller to remain registered; replacement cannot inherit old
execution authority. An authorized Host can cancel the retained reservation and
start a newly authorized activity. Revocation does not silently move the actor or
rewrite old receipts. Claim/release updates NPC runtime/version and invalidates
stale running EVA tasks, preserving existing completed Meta/output semantics.
Individual steps do not bump NPCStateVersion merely for moving a cell.

Existing authored/scheduled `current_activity` prevents start/resume. A Schedule
cannot activate over active navigation. Explicit Game Time may mark an elapsed
schedule missed; after navigation releases control an eligible pending Schedule
may start in the same existing command settlement. Resume then respects its busy
state. There is no Schedule/navigation priority arbitration or silent takeover.

CommandRuntime retains the outer transaction and existing Package-effects,
Schedule, spatial consistency, event persistence, Outbox, world version and
receipt chain. ActivityHandler uses an inner savepoint only to roll back a failed
world primitive before persisting a bounded blocked lifecycle result. SQL failures
abort the outer command and leave no receipt/partial state. A later Package
domain refusal rolls back both primitive and activity changes under the existing
command savepoint and persists only the normal refusal receipt. No inner public
BEGIN/COMMIT or second executor is introduced.

## NPC handoff and evidence

`NPCNavigationIntent(activity_id, target_scene_id, target_cell,
expected_world_version)` is a restricted Result action proposal. Existing Task,
NPC context, controller, Result selection, output record, handoff command guard
and durable CommandReceipt checks all remain. Result submission commits Meta and
pending output only; dispatch separately starts the activity once. Programmatic
advance is a later lifecycle and needs no additional NPC Result per cell. Existing
NPCActionIntent, NPCDoorIntent and NPCCombatIntent retain their meanings.

Actual WorldHandler movement/door events capture existing event-time evidence at
their physical stage. Navigation lifecycle/progress events have empty Signals
and explicit empty sensory scope. A blocked/capability result provides no
fabricated sensory process, OB, Memory, Belief or Goal abandonment. Subsequent
Host cognition is outside this implementation; existing Perception queues and
immutable historical proof remain authoritative.

## Persistence, demonstrations and limits

Migration 22 appends `navigation_activities`, with Session/Actor foreign keys,
historical `(session_id, activity_id)` identity, positive version/nonnegative cursor checks
and a unique active activity per Actor. Schema 1–21 files remain byte-for-byte
unchanged. A real archived RF-1 process creates the compatibility test's Schema
21 database and receipt; current code upgrades it, compares every original row,
replays the old command and verifies the unchanged digest and receipt.

`examples/navigation_activity.py` provides only one high-level destination, uses
the generic bounded driver, closes SQLite halfway, saves its original command
references, and launches a fresh Python process to reconcile and continue. The
original `navigation_station.json` has ordinary shared doors and blockers.
Default NPC/workshop and PC/platform both complete, using different actors and
destinations. `run.json` records the halfway state; `trace.json` records completion,
actual position and receipts. No hand-written per-cell action sequence is used.
Exact executed checks and review findings are in the
[GC-2 Review](reviews/GC2_NAVIGATION_ACTIVITIES_REVIEW.md).

This batch supports public known fine-grid regions, static directed passage
planning, ordinary doors and noncombat execution only. It stops at dynamic
obstacles instead of automatically routing around them. It does not implement
exploration, asking directions, individual map memory, lock/pick/break mechanics,
item/equipment acquisition, sound propagation, combat obstacles, automatic time
costs, full Activity/Goal policy or a production Host/model loop. Active-combat
cross-process continuation remains unsupported. RE/SRD stay pinned at 0.6.0 /
`602dcb8d448e670049427bbb751d6ed226005298`; no dependency upgrade is included.
