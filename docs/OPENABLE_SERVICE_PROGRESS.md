# OpenableService progress (2026-10-17)

Baseline: SM `a8e7a6293b800a15256fa81971f43b1ee0d60553`, branch
`feat/world-openable-service`; clean checkout confirmed against origin/main (0/0).
GC5 stash is excluded and untouched. No extra worktree was created.
No applicable AGENTS.md exists in SM or its ancestor directories.

Design: read `agentic-trpg/docs/reviews/agentic-trpg-architecture-contracts-2026-10-17-v0.4.md`
and the user copy in Downloads (same version label, different SHA256); C05/C06,
C08/C09, Character/World/Combat/Effects ownership and Turn/Round Episode boundaries
apply. Neither filename includes `(1)`. Implementation status in these documents
is historical and is not delivery evidence.

## Scope acceptance

| Phase / scope | Original capability | Current work | Verification/status |
|---|---|---|---|
| 1 ordinary open/close, keyed single/multi-lock, bar, reach/sides | Implemented W1-W8 | Unified in OpenableService | Implemented; 62 focused regressions passed |
| 1 player/NPC/activity/package/check/mechanism sources | Existing sources; mechanism wrote directly | All open writes routed through the same transition validation/application | Implemented; 62 focused regressions passed |
| 1 repeated operations / actual keys | Existing no-op/identity semantics | Retained; explicit query/no-op regression added | Verified (read/no-op regression passed) |
| 2 picking, forcing, unjamming | Not implemented | Typed operations; explicit Package /7 policies; real RE checks and configured consequences | 126 frozen focused regressions passed; RE runtime pinned to `cf808270`; remote/locked install verification pending |
| 2 durability | Internal admitted HP settlement only | Existing; no public attack/damage formula | Integration pending |
| 3 actual fixed RE | `675552406e67470bf3456fe90e88c4f1595bf324` | Inspected installed evaluation_checks: rejects tools/action checks | Dependency work required |
| 3 combat costs, Arcane Lock / Knock | Not implemented | Dependency audit and no-bypass regression only | UNFINISHED; guards retained; not gameplay completion |
| 4 common final validation / behavior coverage | Ordinary writes have final audit; rule consequence path incomplete | Pending | Unfinished |

WorldFacts retains all state and defensive constraints. OpenableService holds no
state cache. Handler owns command flow; the object dispatcher calls the service
without lock callbacks. Mechanisms retain graph/source qualification. Spatial
services retain occupancy calculations and final audits. Existing conservative
restrictions (standing points, public scene, active effects, combat refusal) are
engineering coverage limits, not D&D rules.

## Rule sources

Official [SRD 5.2.1](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf),
verified online on 2026-10-17: Tools pp.93-94; Crowbar p.97; Lock p.98;
Arcane Lock p.110; Knock pp.143-144. Ordinary locks use Dexterity/Sleight of Hand
DC15 with Thieves' Tools; tool proficiency and applicable skill interact under
the revised tool rules. Crowbar advantage requires applicable Strength leverage.
Arcane Lock forbids nonmagical unlocking (no 2014 +10 DC); exceptions and password
exist. Knock unlocks one of multiple locks and suppresses Arcane Lock for ten
minutes; unlocking does not itself open the object. Sound belongs to Events.

## Validation log

Phase 1: 62 tests passed in 21.01s, including real checks, rollback and fresh-process
recovery. Ruff check and format pass for changed Python files. Initial pytest invocation failed in temporary
fixture creation due to sandbox default-temp permissions; rerun uses repo-local
`--basetemp`. This is not a behavior failure or passing evidence.


## Phase 2 implementation notes

`PickLock(scene_id, object_id, lock_id, tool_instance_id, expected_cell)`,
`ForceOpen` / `ClearJam(scene_id, object_id, method_id, tool_instance_id, expected_cell)`
use ordinary player/NPC command authorization. No caller-supplied DC, success,
trusted flag or state patch exists. Tool-less force methods use an explicit null
instance. Special locks require configured DC; ordinary SRD locks select srd_lock.
Legacy keys and Package /1-/6 retain their raw Package digests and do not gain
invented picking policies. Extended definitions require Package /7.

Each force method declares ability, optional skill, DC, optional required tool,
leverage applicability, and the exact lock/bar/jam/open consequences. Failure
causes no tool damage, jam, trap, or invented HP damage. Successful unlocking and
unjamming do not imply opening. Every attempted open still checks remaining
obstacles and spatial occupancy. HP remains exclusively in ObjectDurabilityService.
Directional jam policies are immutable definitions; existing rows remain valid,
with prevent_transitions as the default. No storage migration is necessary.

RuleExecution now owns the existing shared prepare/evaluate/commit function;
CombatHandler delegates. Object checks reuse its receipts, RNG, savepoint, events,
OCC, and final spatial validation. Invalid operations and conflicts do not draw or
pay. Valid failed checks commit their actual CheckRolled and RNG transition.
Already-completed operations do not evaluate or emit another event.

RE branch starts at the actual pinned dependency, not latest main. engine-openable/1
is an additive typed RE API using the same immutable snapshot, binding, result
and RNG contracts; generic rules.check retains its previous admitted range.
Availability uses the identical draw-free RE capability validator.


Phase 2 frozen focused gate: 126 passed in 41.93s; Ruff check and format pass
(150 Python files). Additional new behavior tests total 19, including actual
unjamming Signal -> ordered Perception -> authorized NPC Observation. The broad
RE integration gate is still pending; no complete dependency validation claimed.


Phase 2 dependency code commit: RE `cf808270e5778fbde91fc5077f862b568b5774d5`
on `feat/world-openable-support`, based on the previously fixed `67555240`.
Both Engine and SRD source references are changed together in pyproject/uv.lock.
Focused development used that source checkout via PYTHONPATH; actual locked
installation and frozen combined validation must run after the RE push, before
final delivery. RE full engine rerun remains pending at this phase commit.


## Phase 3 - dependency audit and integration boundary (NOT COMPLETE)

The requested combat/magic gameplay is not implemented by this increment. These
are concrete missing dependencies, not statements that the requested rules are
unsupported by D&D:

| Required capability | Actual missing contract/owner | Status |
|---|---|---|
| World object interaction / Utilize / Fast Hands | RE has no object interaction payment request; SM has no free interaction ledger or admitted object budget delta | Unfinished |
| Combat open/close on blocking doors | `_spatial_combat_guard` refuses closed passage doors; `_combat_snapshot` does not project dynamic door environment | Dependency blocked |
| Turn/Round Episodes | No Episode implementation exists in src; EventCapture still has per-event attack signals | Dependency blocked; no new combat sensory pipeline added |
| Arcane Lock / Knock casting and effects | Fixed RE uses UtilityActivity data but SM admits only basic attack combat intents; no admitted world-target spell result or object Effects lifecycle exists | Unfinished |
| Suppression expiry / password minute | v0.4 C12 requires TimeBinding; game-clock/1 still stores abstract ticks without conversion. Old ticks cannot be relabeled seconds | Dependency blocked |
| Actual object attack / addressable lock HP | No RE object-target attack settlement ingress. Existing internal durability settlement does not implement attacks or separate lock HP | Unfinished |
| New NPC EVA picking/forcing intents | Trusted NPC commands use the shared service, but NPC Result DTO/handoff has no new picking/forcing vocabulary | Unfinished |

Stage 3 adds consistent rejected receipts for object-check commands during actual
active combat; no evaluation, RNG, budgets, events or world state are adopted.
This is refusal/atomicity coverage, not completion of combat costs. Unknown trusted
or bypass fields continue to be rejected by the public DTO parser. Existing
player/NPC/activity/package/mechanism open/close routes remain unified.

To finish the original scope, implement admitted combat object payment and
geometry, Episode delivery, explicit TimeBinding migration, and world-target
spell/effect deltas together with their owners. Do not close these checklist items
or claim Arcane Lock/Knock, combat costs, or real object attacks based on these
refusal tests. No new placeholder spell interface is introduced.
