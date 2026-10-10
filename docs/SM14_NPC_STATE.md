# SM-14: persistent NPC state and authorized views

Architecture checked against Meta main `fc41d3c584816198ba4f23bb4d326655ea3c774d`
on 2026-10-10: Module Contracts §§7/9/10, State Machine Architecture
§§3.4/3.8–3.9/6, Agent Architecture §§4–6/8, Adventure Package Schema §§5/9,
and MVP Scope. Module Contracts `[DECIDED]` is the highest authority. The removed
ADR is not a current override. Proposed field encodings remain local choices.

## Ownership and lifecycle

`actors(session_id, actor_id)` remains the only NPC identity/location authority.
`npc_states(session_id, npc_id)` is its one-to-one private domain state, not a
second WorldState or LLM Context. A configured state stores name, static Profile,
controller metadata, runtime Schedule/Current Activity and NPCStateVersion.
Typed Memory, Belief, Relationship, Goal and Plan records carry stable IDs,
content versions, current markers and explicit provenance. The view includes all
versions, including retracted Beliefs. It does not infer truth from text.

Session creation persists approved static World Facts and the entry Scene's
necessary NPC states in the existing creation transaction. Later NPCs are created
only when their initial Scene first instantiates the existing Actor. The successful
Actor INSERT is the initialization fence. Scene references, revisits, NPC movement,
Package reloads and resume never reseed private records. Relationship targets may
reference approved NPC identities before those Actors are instantiated; this does
not create or expose the target's private state. `selected_pc` resolves to the
Session's generated PC Actor ID, not an unselected template.

All writes remain in existing short SQLite transactions, including creation
deduplication. A failed NPC insert or deferred source FK commit rolls back Scene,
Actor, NPC state, facts, RNG, events, Outbox and receipt/creation mapping together.
There is no model, network or Rule Evaluation inside these transactions.

## Local Package extension: `npc-state/1`

Existing `state-machine-foundation/1` Package files retain their original digests
and remain accepted without new fields. Optional `actors[].npc_state` has exactly:

| Field | Local encoding |
|---|---|
| `schema_version` | `npc-state/1` |
| `profile` | role, explicit personality strings, speaking_style, constraints |
| `initial_knowledge_ids` | Approved, explicitly NPC-authorized World Fact IDs |
| `memories` | Stable `memory.*` IDs, versions, current marker, text, epistemic_status, sources |
| `beliefs` | Stable `belief.*` IDs, versions, current marker, text, epistemic_status, confidence_basis_points or null, active/retracted status, sources |
| `relationships` | Stable `relationship.*` IDs, target_id, friendly/neutral/hostile stance, text, versions, sources |
| `goals` | Stable `goal.*` IDs, active/completed/abandoned status, text, versions, sources |
| `plans` | Stable `plan.*` IDs, same-NPC goal_id/goal_version, steps, status, text, versions, sources |
| `runtime` | Explicit schedule entries and nullable current_activity |

All records have `record_id`, positive integer `version`, Boolean `is_current`,
bounded `text` and nonempty `sources`. Histories start at 1, are contiguous, and
have exactly their latest version marked current. Initial histories are reviewed
Package data, not fabricated Session evaluations. Initial and future Belief versions
share the same record model; there is no separate initial-only Belief type.

Sources currently supported at initialization:

- `package`: reference_id must equal the owning NPC ID, version must be null.
  Together with the view's pinned package_digest this identifies the approved
  private template. Package-authored pre-Session backstory is not an Observation.
- `memory`: reference_id/version must resolve to this NPC's own Memory in this
  Package template. Actual provenance graph cycles are rejected.

Session Event/Observation references are rejected in Package templates: they do
not exist before creation. Runtime evidence-source encodings and their authorization
will be reviewed with Evaluation Task/Atomic Meta Commit; this Batch has no runtime
Meta writer and does not claim that contract is frozen. The provenance/record
structure is reusable, with new source variants subject to explicit schema review.

`world.world_facts` may now contain bounded `{fact_id, text, visibility,
authorized_npc_ids}` entries. `dm_only` requires an empty NPC grant list.
`npc_authorized` grants identify which NPC may initially know that fact. Private
Known Facts are a subset referencing SM-owned facts, never the whole Package.
An incorrect Belief is legal and never updates these objective facts. This Batch
does not add dynamic fact or knowledge writes.

`controller` accepts only `{kind: sub_agent|deterministic_policy|scripted_event}`.
These are metadata only: no policy, script, Agent or controller is executed.
Unknown fields and unsupported mechanisms are rejected. Missing NPC Profile
produces `configuration=unconfigured`, nullable Profile/Runtime and empty private
records; it does not manufacture personality or decision capability.

Limits: existing 1 MiB Package, 128-character dotted IDs and 4096-character text;
100 entries per record category/fact/knowledge list, 16 Profile traits/constraints,
Plan steps and Schedule entries, at most four unique sources per record.
Confidence is nullable integer basis points 0–10000, preserving the existing
no-float canonical Package encoding. Schedule ticks are integers 0–1000000,
start < end, with approved Scene references; they are data, not a frozen clock API.

## SQLite Schema 10

| New table | Purpose and constraints |
|---|---|
| `npc_states` | PK Session/NPC; FK existing Actor; NPC-kind trigger; configuration, schema, bounded Profile/Runtime JSON and positive NPCStateVersion |
| `world_facts` | PK Session/fact; SM objective static text, visibility and explicit NPC grants |
| `npc_known_facts` | PK Session/NPC/fact; FKs to state and fact; grant/visibility trigger rejects other-NPC and DM-only knowledge |
| `npc_private_records` | Closed five-kind domain records; PK Session/NPC/kind/ID/content version; one current version per ID; bounded typed JSON; immutable historical content and no deletion |
| `npc_record_sources` | Owning-record FK; same-Session/same-NPC Memory version FK; explicit Package ownership; bounded ordered sources; immutable provenance |
| `npc_plan_goals` | Same-Session/same-NPC Plan and Goal version FKs, including deferred validation within initialization |

Typed payload limits are 131072 characters; Profile limit 262144 and Runtime limit
131072. Whole-Package and list limits apply before any transaction. Per-record
`npc_state_version` records the initial domain revision (1), independently from
content version, world_version and event_seq. This Batch does not advance Meta
versions at runtime or freeze future Meta-only world version policy.

Migration preserves Schema 1–9 authoritative data, Package pins/digests, receipts,
events, Outbox/Perception and RNG. Every already-instantiated NPC gets an explicit
`initialization_origin=legacy`, `configuration=unconfigured` state at version 1,
with unknown name/controller/Profile/Runtime and no invented cognition. Migration
does not read Package files, synthesize observations or reinterpret saved history.
Source schema is rechecked under the existing cross-process migration lock; all
steps and FK checks commit together. Failed/interrupted upgrades retain the old
schema and retry cleanly. There is no `IF NOT EXISTS` repair for incomplete Schema 10.

## Authorized read

```python
from trpg_state_machine.perception import NPCContext

# Construct only in a trusted authenticated adapter. The Session owner first
# uses the existing grant_npc_access to bind the persisted principal/observer.
view = runtime.get_npc_view(
    session_id,
    "npc.caretaker",
    approved_package,
    context=NPCContext("agent:npc.caretaker", session_id, "npc.caretaker"),
)
```

The existing `_npc_authorized` boundary rechecks Session, Principal, Observer,
persisted grant and pinned Package/Ruleset in the same read transaction. Requesting
another NPC, a revoked grant, an owner-only TrustedContext or payload-shaped dict
is rejected. Each SQL query is scoped to exactly that Session/NPC. The detached
immutable NPCView includes the complete authorized domain state, history and
version, and reads current_scene_id from `actors` rather than storing another
position. It excludes other NPC Meta, DM-only truth, raw Package and event log.

Use existing `read_observations`/`get_scene_view` separately for Perception.
`get_npc_view` does not ACK, delete Inbox entries, advance any cursor, complete
cognition or grant mechanical/NPC command control. It performs no semantic
retrieval, ranking, summarization or Context compaction.

## Next Batch boundaries

Not implemented: NPCEvaluationTask, EvaluationReceipt, seven-field Result submit,
Atomic Meta Update, Mode switching, Gate, Schedule Tick, LLM Runtime/Prompt Builder,
Memory/Belief generation, Action Intent handoff, additional ACK or runtime
Event/Observation-backed Meta writes. No proposal_id, mode_epoch or parallel NPC
state was introduced. A later Task Batch must authorize/serialize evaluations,
revalidate state/evidence/versions and atomically append histories with reliable
output handoff under the reviewed contracts. There is no Host overwrite API.
