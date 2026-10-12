# GC-1 revised implementation plan

Baseline: `058e79f300016f7397f0c3401bc03b58554e2b71`, clean existing
`feat/sm-gc1-world-interactions`, fetched origin/main at the same revision.
Only this SM checkout is writable scope. No applicable AGENTS.md was found.
The revised coordinate task supersedes the initial abstract-placement design.

First converge on Scene + canonical RE Cell. Existing actors/objects rows own
current positions; mechanical_scenes owns the local base Grid. Adapt exploration
evidence and combat projection before introducing the operations. Keep current
positions distinct from frozen terminal combat records.

Then add typed adjacent StepActor, endpoint-bound TraversePassage and single-cell
PlaceObject. Generate an ephemeral movement-only topology from authoritative
occupants/doors; leave base visual/mechanical geometry intact. Package /2 pins
grids, cells, directed endpoints and object capabilities. Schema 21 adds the used
columns and zero-visit initialization. The user confirms there are no legacy saves:
coordinate recovery/backfill/conversion is outside scope. Existing /1 digest and
historical receipt regressions remain intact.

Intentionally change MoveActor from arbitrary relocation to ordinary exit traversal,
using the same passage decision as EnterScene, compatibility helpers and NPC handoff.
Keep committed historical receipts replay-only. Support only public, perceptually
clear noncombat placement; complex conditions fail closed. Queries expose filtered
targets and generic passage outcomes, with no writes or hidden obstacle identities.

Audit combat and perception consumers: reuse current actor cells for combat;
reject movement-only obstacles the locked evaluator cannot represent. Exploration
evidence freezes actual-stage actor/object cells. Existing world effects can only
toggle declared switches/flags, not relocate actors or objects. Ordinary movement
does not add time, combat costs, cover, visual or acoustic obstruction.

Validation: repository Fast tier first, focused real SQLite GC-1 cases (two objects,
directions, gates, multiple obstacles, grants, restart in a new process, replay,
conflicts, savepoint rollback and response loss), then full SM suite, Ruff and format.
Add a repeatable public-interface example, support/design notes and independent diff
review before explicit staging, commit and push. Do not merge main.

Read-only architecture reference: adjacent control checkout has unmerged evolution
documents distinguishing its proposals from main; the user's present SM + RE
decision governs this task. No Evennia migration or dependency is authorized.
See the implemented [spatial contract and limits](GC1_SPATIAL_WORLD_INTERACTIONS.md).
