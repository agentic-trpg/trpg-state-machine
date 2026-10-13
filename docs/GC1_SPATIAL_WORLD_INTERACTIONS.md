# GC-1: coordinate-based world interactions

This is the implemented, bounded SM-local candidate `spatial-world/1`, not a frozen
cross-repository ABI. The revised GC-1 request governs this implementation. A single
commit may include both the spatial foundation (A) and operations built on it (B).
No LLM is called; these are composable execution primitives for a later runtime.

## Facts and authority

Position is `(session_id, scene_id, canonical_cell_id)`. Cell uses the installed
RE's `"col,row"` encoding, zero-based local Grid coordinates and `cell_size_ft`.
The reference frame is fixed to the Scene, independent of PC location or viewport.
One fine Scene has one Grid (at most 4096 cells); a Scene may contain several rooms.
Bare cells from different Scenes never enter a shared distance/path calculation.

| Fact | Single authoritative source | Consumer |
| --- | --- | --- |
| Actor identity, Scene and current Cell | `actors.current_scene_id, cell` | exploration, combat projection, event capture |
| Object identity, Scene, current Cell and switch state | `objects.scene_id, cell, state` | movement projection and source evidence |
| Base mechanical/visual geometry | `mechanical_scenes.state_json.grid`, marked `spatial_enabled=1` | RE topology, combat and perception |
| Directed exits, two endpoints, shared door identity, object capabilities | pinned immutable Package /2 | admission and projection configuration |
| PC current Scene | existing `sessions.current_scene_id`, updated atomically with PC actor | session lifecycle |
| Current combat positions | read projection from actor rows; no active stored `actor_zone` | RE request snapshot |
| Ended combat positions | terminal `combats.state_json.actor_zone`, frozen at close | historical record only |
| Event-time positions | versioned staged Evidence, immutable | asynchronous perception |

All fine maps and their initial entities are instantiated once at creation, so an
NPC's remote Scene exists before the PC visits. Unvisited fine Scenes start with
`visited_count=0`; the entry starts at 1. Revisit/resume never reapplies authored
positions. There is no blocker table, parallel entity catalogue or persistent path
cache. Ordinary object placement changes one Cell, from which blocking is derived.

## Configuration and operations

[`examples/spatial_station.json`](../examples/spatial_station.json) is a complete
original CC0 fixture. `state-machine-foundation/2` adds the required root `passages`;
each Scene requires `grid` (null for abstract), each exit `passage_id` (null only
for abstract), actors/PC templates `initial_cell`, and objects the exact fields
`object_id,state,movable,blocks_passage,initial_cell`. A non-switch object has null
state. `blocks_passage` requires `movable`; in this candidate it means a single
Cell blocks ordinary movement wherever placed, including a passage threshold.
Grid uses the installed `MechanicalGrid` closed schema, not arbitrary extra fields.
Configured mechanical foundation grids, including additional Scenes, must agree.

Each passage has exactly two distinct Scene endpoints with `standing_cell` and
adjacent `threshold_cell`, plus optional `door_object_id`. An exit explicitly
chooses direction; two exits sharing one passage identity share one door state.
Removing a reverse exit makes the route one-way. Names do not imply relationships.
Traversal direction does not restrict door controls: every configured endpoint of
a shared door is a local control port, even without an outgoing exit. The object
row may live in either endpoint Scene; its physical Cell remains in that Scene.
There is no duplicated door or synchronization. This candidate has no single-sided
shared-door control option. An unbound fixed switch is local to its storage Scene;
it does not implicitly control a passage. A future single-sided mechanism needs
an explicit configuration contract rather than relying on storage location.

| TypedCommand operation | Local payload | Admission |
| --- | --- | --- |
| `world.step_actor` / `StepActor` | Scene, expected Cell, target Cell, `actor-step/1` | one adjacent legal step; same Cell is validated no-op |
| `world.traverse_passage` / `TraversePassage` | passage, source Scene, expected Cell, `passage-traversal/1` | actual source standing point; both threshold edges and destination standing point free |
| `world.place_object` / `PlaceObject` | object, Scene, expected Cell, target Cell, `object-placement/1` | visible public local object, trusted capability, clear adjacent reach to source/target and legal adjacent object shift |
| Existing `EnterScene` / `MoveActor` | unchanged legacy fields | fine traversal uses the same endpoint validator; no arbitrary NPC relocation |
| `set_object_state` / `SetObjectState` | interaction Scene, shared object ID, typed open/closed state | controlled PC/NPC uses its own visible local port, exact standing point and clear threshold edge; other switches remain local PC-only |

Object placement excludes only the handled object from occupancy. A closed door's
threshold can be handled from its own declared standing point; other door cells,
walls, actors and objects remain blockers. Clearing one of several blockers does
not clear the others. Manual shared-door operations validate actual actor state,
visibility and reach to that side's threshold, independently of the door's storage
Scene/Cell and the PC's current Scene. Other obstacles remain in the reach Grid;
no foreign bare Cell enters the local calculation. A shared door cannot close onto
an actor at either threshold, for manual or automatic execution.

Package/conditional world effects retain their closed switch/flag subset and the
same transactional executor, with a private, mutually exclusive execution origin:

| Origin | Established by | Reach and scope |
| --- | --- | --- |
| Actor | authorized TypedCommand or persisted NPC handoff | actual actor Scene, supported actor state, visibility and manual reach |
| Package | pinned definition, trigger history and satisfied condition | current Scene and actual object storage Scene; no actor reach requirement |
| Check consequence | trusted GM grant, fixed proposal/preconditions, validated actual RE outcome | existing current/storage Scene scope; no actor reach requirement |
| Synthetic delta | validated closed rule result and Delta | existing supported flag subset and Scene scope |

Missing or mixed origins fail closed. `trusted`, `source` or `execution` strings
in client payloads confer no authority and fail the closed decoder. Automatic
effects never fall back to the PC. They still validate types, declared targets,
preconditions, switch capability, both threshold occupancies, active-combat support,
versions, transactions and idempotency. Manual remote opening remains rejected.
Automatic effects cannot use a shared door's far-side interaction alias: their
previous cross-Scene scope is unchanged. None can assign coordinates.

Base geometry and a temporary movement Grid are distinct. `_movement_topology`
adapts authoritative occupants and closed thresholds into a transient RE topology
for reach/edges/corner checks. That topology is never saved or passed to combat or
perception. A movement-only crate does not gain visual cover or acoustic isolation.
Object/source Cell validity uses canonical bounds; actor standing additionally
uses walkability. An object on a static blocked Cell is a valid located source;
actual sight still follows the base RE geometry.

## Authorization, transactions and evidence

The adapter provides authenticated `TrustedContext` control grants or persisted
`NPCContext` grants. Request data carries neither credentials nor capabilities.
`read_actor_position` is own/control-scoped. `get_spatial_view` filters visible
local objects and endpoints using public scope, declared NPC visual capability
and base geometry; it is unavailable during active combat. It does not export a
GM map, private NPC state or hidden obstacle identities. `query_world_action`
shares execution admission and returns a generic reason plus world version.
The current role-spatial query set is StepActor, EnterScene, MoveActor,
TraversePassage, PlaceObject, SetObjectState, and the GC3/GC4 item/lock payloads.
SetSceneFlag is not a spatial query: it explicitly raises `unsupported_operation`;
formal Flag submission and approved automatic effects retain their support.
See [GC14 review fixes](reviews/GC14_REVIEW_FIXES.md) for the public red/green proof.
`SpatialView.doors` exposes each visible local port's shared object identity,
current state and local standing/threshold Cells; it never substitutes a remote
storage Cell for a local Cell. It is independent of the directed `exits` list.
Hidden/nonexistent local object and fine exit references use `invalid_target`.
Mechanical visibility authorizes this explicitly public interaction subset; it
does not assert that an NPC has actually noticed the stimulus.

The GC14 repair captures NPC cross-Scene movement as `actor.departed` before
position update in the source Scene, then existing `actor.moved` after arrival.
PC `scene.exited` / `scene.entered` semantics remain unchanged. Both NPC stages
share one command/transaction, with actual frozen candidates and coordinates.
Departure Signals do not expose destination or navigation data. Candidate status
alone never creates a Positive Observation; old moves/receipts are not backfilled.

Formal execution rechecks current position, actual conditions and OCC under the
existing short transaction. Actor Scene/Cell, PC Scene, object Cell/state, world
version, staged events/Evidence, Outbox and CommandReceipt commit together. A
world-command domain rejection rolls back the effect savepoint and stores the
rejection receipt; storage faults roll back the whole transaction. Conditional
checks retain their existing narrower durable rejection policy: other effect
validation failures roll back the whole transaction without a receipt. No-op
creates only the receipt.
Same key/input replays, same key/different input conflicts. Old committed receipts
are replayed without executing historical movement under the new admission.
NPC reliable output uses its existing durable handoff and command identity.
`NPCDoorIntent` adds only local shared-door open/close to that handoff. Task identity,
persisted controller and current control grants are rechecked before dispatch and
execution. Unsupported switches produce a rejected output with no command;
remote/blocked actions receive the actual rejected command receipt. NPC Flag and
general switch/world-write authority are not added. This is not an NPC Policy.
World moves use no RNG, do not advance game time and consume no combat action.

For spatial/null-state object Packages, new producers freeze `event-evidence/5`
actor and source-object Cells at the actual stage. PC departure is captured before
moving, arrival after; object adjustments capture after mutation. Older Evidence
retains its original parser and history, with no current-state refill. Existing
Positive/Negative/Unresolved and NPC queue/barrier semantics remain in force.
Cross-Scene sensory propagation and RE internal event-stage evidence remain open.
Shared-door manual signals originate from the real acting character in the
interaction Scene, capturing its current local Cell at execution. Automatic door
signals originate from the actual object in its storage Scene, with the verified
origin reference in the event payload. They never invent a PC move or manual act.
Existing events, receipts, Outbox, OB and Episode/EVA architecture are retained.

## Installed RE support matrix

Dependencies are unchanged: Engine and SRD `0.6.0`, locked revision
`602dcb8d448e670049427bbb751d6ed226005298` in `pyproject.toml` and `uv.lock`.
The implementation was checked against the installed `.venv/Lib/site-packages`
sources, rather than newer RE main APIs.

| Capability | Actual use / boundary |
| --- | --- |
| `canonical_cell_id`, `GridTopology.distance_ft` | canonical validation and bounds, independent of walkability |
| `GridTopology.is_valid_cell`, `edge_distance` | standing, walls, adjacency and diagonal corner legality |
| `shortest_path`, `can_see`, LoS/cover helpers | upstream algorithms; no copied A*, line rasterizer or distance formula |
| `MechanicalGrid`, `GridScene` | closed storage validation and topology adaptation |
| Stateless basic attack | real installed evaluator accepts only its existing simple bright Grid subset |
| Movement-only dynamic objects / closed passage doors | rejected for affected encounter initialization; cannot be represented without changing mechanics |
| Generic noncombat checks / objects in other Scenes | retain existing support; no Session-wide blanket rule disablement |
| `PositionDelta`, combat movement or object physics | not implemented; no new rule executor or cost formulas |

Fine combat preparation projects current actor Cells and requires the complete
present roster. Commit checks preparation world version and positions again.
Active combat stores no independently mutable current position map. Closing first
freezes the actor map into the terminal record; later exploration cannot rewrite
it. Dead/incapacitated/effect-bearing actors and active fine NPC Activities require
unsupported adjudication and cannot move. Active-combat world movement/switches
remain rejected; exact active-combat cross-process continuation is not expanded.

## Storage and exclusions

Schema 21 adds nullable actor/object Cell columns and the mechanical Scene marker,
permits null switch state, and rebuilds Scene visit count with `>=0`. Migration is
transactional under the existing cross-process lock with FK/integrity validation.
Existing Package /1 raw canonical digest and old operation dataclass layouts remain
unchanged. Old objects do not acquire movable/blocking capabilities.

The user confirms there are no legacy saves. This batch provides no historical
coordinate reconstruction, authored-template backfill, old-combat coordinate
conversion or world-format migration. Abstract Scenes remain explicit; fine and
abstract traversal cannot silently drop coordinates or invent a destination.
Existing schema/receipt history regression tests remain, including interrupted
DDL, without introducing a legacy coordinate recovery workflow.

Excluded: world XYZ, multiple/shared Grids per Scene, complex shapes/rotation,
inventory carrying/weight rules, combat movement, automatic time costs, Activity
execution, full NPC AI, cross-map sight/sound, Episode/EVA redesign, scripts/eval
and a model-based semantic experience.

## Repeatable execution and references

From the repository root, using fresh output directories:

```powershell
uv run --offline --locked python examples/spatial_world.py --directory .test-tmp/spatial-crate
uv run --offline --locked python examples/spatial_world.py --directory .test-tmp/spatial-stool --object stool
uv run --offline --locked python -m pytest -q tests/test_spatial_world.py
uv run --offline --locked python -m pytest -q tests/test_gc1_doors_and_effects.py
```

The example sends structured closed envelopes through `world_command_from_data`
and public command APIs: move, block, refuse PC/NPC traversal, clear, resume both
directions, close/open the same door from B as PC and A as NPC, and verify exact
state including door states in a fresh interpreter. It keeps Package, SQLite
and `trace.json`; existing directories are never reset. Its local trusted Host is
an example authentication boundary, not production identity infrastructure.
The independent real-RE continuity test is
`test_real_re_positions_survive_initialize_close_and_exploration`; it uses an
obstacle-free supported encounter and the real evaluator, not a simulated result.
Executed validation and independent review are recorded in
[GC-1 review](reviews/GC1_SPATIAL_WORLD_INTERACTIONS_REVIEW.md).
The subsequent F01/F02 corrections and their distinct validation ledger are in
[premerge fix review](reviews/GC1_PREMERGE_FIX_REVIEW.md).

Source references (read 2026-10-12):

- [Evennia XYZGrid](https://www.evennia.com/docs/latest/Contribs/Contrib-XYZGrid.html):
  separate local maps and explicit transitions informed the Scene/map boundary.
  This implementation imports no Evennia framework or XYZ global position system.
- [CDDA Activities](https://docs.cataclysmdda.org/PLAYER_ACTIVITY.html): durable
  activities can outlive an immediate action; its coordinate warning supports a
  reference frame independent of the avatar. This design uses stable Scene-local
  coordinates and does not implement CDDA's activity scheduler.
- Locked RE sources:
  [spatial.py](https://github.com/agentic-trpg/trpg-rules-engine/blob/602dcb8d448e670049427bbb751d6ed226005298/packages/dnd5e-engine/src/dnd5e_engine/spatial.py),
  [specs.py](https://github.com/agentic-trpg/trpg-rules-engine/blob/602dcb8d448e670049427bbb751d6ed226005298/packages/dnd5e-engine/src/dnd5e_engine/specs.py),
  [evaluation_state.py](https://github.com/agentic-trpg/trpg-rules-engine/blob/602dcb8d448e670049427bbb751d6ed226005298/packages/dnd5e-engine/src/dnd5e_engine/evaluation_state.py),
  [evaluation_preflight.py](https://github.com/agentic-trpg/trpg-rules-engine/blob/602dcb8d448e670049427bbb751d6ed226005298/packages/dnd5e-engine/src/dnd5e_engine/evaluation_preflight.py).
- SM implementation:
  [spatial.py](../src/trpg_state_machine/spatial.py),
  [package.py](../src/trpg_state_machine/package.py),
  [combat.py](../src/trpg_state_machine/combat.py),
  [events.py](../src/trpg_state_machine/events.py),
  [migration 21](../src/trpg_state_machine/_migration_21.sql).
