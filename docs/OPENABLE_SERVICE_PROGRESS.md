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
| 2 picking, forcing, unjamming | Not implemented | Pending | Unfinished |
| 2 durability | Internal admitted HP settlement only | Existing; no public attack/damage formula | Integration pending |
| 3 actual fixed RE | `675552406e67470bf3456fe90e88c4f1595bf324` | Inspected installed evaluation_checks: rejects tools/action checks | Dependency work required |
| 3 combat costs, Arcane Lock / Knock | Not implemented | Pending | Unfinished; guards retained |
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
