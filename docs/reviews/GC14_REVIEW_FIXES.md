# GC1–GC4 review fixes: spatial queries and NPC departure evidence

## Actual baseline and narrow scope

Repository: `D:/Projects/agentic-trpg/trpg-state-machine`, origin
`https://github.com/agentic-trpg/trpg-state-machine.git`.
Existing branch: `feat/sm-gc4-lockable-passages`.
Clean actual starting HEAD and fetched feature branch:
`359e096f1621ecb4b6cbbd55c9320e1e2efe5701`. Fetched main remained `1098bd7`.
No applicable repository/ancestor AGENTS.md was present. No reset, stash, rebase,
amend, dependency upgrade, other repository write, main merge or next batch.

Read GC1–GC4, RF1, RF_RE4 and RF4 position-source repair documentation and audited
the current WorldHandler/commands, EventCapture/EventPersistence, perception
admission/queue, ActivityHandler and NPC output paths. Only GC14-REV-01 and
GC14-REV-02 are repaired. The production change touches two files; storage stays
Schema25, Package /5 and Evidence /4-/5. Existing migrations, fingerprints,
dependency pins, perception/Observation/Episode architecture and gameplay scope
are unchanged. The referenced unrun draft is not used as validation evidence.

## GC14-REV-01: query admission and dispatch

The original public `StateMachine.query_world_action` admitted a real
`SetSceneFlag(A, flag, True)`, then `_world_destination` accessed its nonexistent
`to_scene_id`, raising AttributeError. The valid Package/real SQLite regression
reproduced this before any production edit. Caller/document audit found no
explicit supported Flag spatial-query contract or production caller requiring it.

The minimal repair removes SetSceneFlag, ObjectState and the FoundationOperation
type alias from this query's supported payload tuple. They remain imported/defined
where normal execution requires them. Unsupported inputs now raise the existing
`StateMachineError("unsupported_operation", ...)` at entry. There is no general
registry, exception catch-all or invented available result. Formal Flag commands
and approved Package/Check effects keep their existing behavior.

The current closed role-spatial query set is StepActor, EnterScene, MoveActor,
TraversePassage, PlaceObject, PickupItem, DropItem, SetObjectState, UnlockDoor and
LockDoor. Real public tests cover success and rejection for every type, compare
query admission with command receipt admission from the same facts and compare
every persisted table before/after the query. Unknown/remote object references
share a generic target refusal. Separate tests check explicit unsupported types
and successful ordinary Flag submission with its original automatic consequence.

## GC14-REV-02: one move, two actual stages

Before repair, a valid NPC MoveActor A→B committed only `actor.moved`. The red
regression expected source and destination events and observed that missing
departure. The repair stays in `_operate_spatial`, after successful destination
validation and the no-op guard, before destination initialization/position update:

| Actor / phase | Semantic event | Frozen scope / source |
|---|---|---|
| NPC before position update | `actor.departed`, `world.before_actor_departure` | Actual NPC in A, actual source Cell and A geometry/candidates |
| NPC after position update | Existing `actor.moved`, `world.after_actor_move` | Actual NPC in B, arrival Cell and B geometry/candidates |
| PC before / after | Existing `scene.exited` / `scene.entered` and original phases | Original PC identity, payload, Session Scene and visit semantics |

EventCapture adds the generic visual Signal “Someone leaves the scene.” for the
new NPC event. Its origin is the actual moving NPC and scope is A. The departure
payload includes source Scene/Actor/Cell only, with no destination or plan. JEV
still receives only filtered Signals and frozen observer/environment evidence,
never raw movement audit payload. Tests inspect that input directly. Discovery
continues to supply candidates; it is not Positive perception. Explicit test Host
judgments are labeled synthetic, and known sensory denial never invokes them.

MoveActor, explicit TraversePassage, GC2 navigation and the retained `_apply`
compatibility helper reach this same implementation. StepActor, same-position
no-op, failed range/route/lock admission and blocked navigation do not depart.
NPC movement never switches `sessions.current_scene_id` or increments PC visits.
The original abstract NPC restriction on uninitialized destinations is retained:
the compatibility test first proves refusal without departure, initializes through
ordinary PC visits, then checks both NPC stages without an additional visit count.

CharacterFacts remains the current position source. EventCapture freezes full
available geometry and actual coordinates at each stage, retaining unknown
metadata where appropriate. No active actor_zone or historical-position fallback
is added. Three-NPC fine-grid tests distinguish A's observer, B's observer and
the mover; abstract Evidence /4 and actual D&D Evidence /5 are also exercised.
Later movement, actual pinned-RE death/closure and reopening preserve old Evidence.
Historical projection forbids current-position access; SQL trace also checks that
current geometry/mechanical position is not queried to repair the past.

## Atomicity, ordering, compatibility and bounds

Departure capture, position, arrival capture, activity cursor, world version,
events, Evidence, Outbox/queue proofs and receipt use the same existing short
transaction. EventPersistence assigns sequential identities from actual staged
order; queue candidates come from frozen Discovery. No fixed event offset is
introduced. Existing NPC handoff remains pending until dispatch and replays one
receipt containing one physical pair, including concurrent dispatch.

Real SQLite fault triggers cover position UPDATE and Evidence/Outbox/receipt
INSERT. The position fault proves source Evidence was captured before the later
failure; all persisted tables still match exactly and retry commits once. Another
test captures both physical stages, injects a domain failure before return from
the activity step, and proves the inner savepoint retains only the blocked
lifecycle event with unchanged position/cursor. Explicit resume then produces the
real pair. Committed response loss and reopen replay the original pair without
new events. RNG and game time retain existing exploration semantics.

The original 32-event cascade limit is unchanged. An approved actor.moved cascade
exceeding it rolls back source/destination stages, position, trigger history and
all world changes, retaining only its ordinary refusal receipt. Arrival trigger
history still links to the real arrival event despite the added source event;
the test also proves PC-current-Scene gating still suppresses offscreen effects.
The new departure type is not added to Package's configurable trigger list.

A separate process imports an actual `git archive 359e096` runtime and creates an
original one-event NPC move/receipt. Opening and replaying it with current code
leaves every stored row unchanged. Only a new real move adds the two stages.
Package digest and old command fingerprint therefore remain compatible; no old
NPC departure is backfilled.

## Executed red, green and regression evidence

The initial fixture setup attempts failed Package checks for inherited NPC
knowledge/template references and overlapping alternative PC positions. These
were fixture errors, not defect reproduction. The final fixture uses approved
public content, distinct positions and no inherited private knowledge.

Valid pre-fix public red command:

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=.test-tmp/gc14-red-api tests/test_gc14_review_fixes.py
```

Result: **2 failed in 2.20s**, with the actual Flag AttributeError and missing
actor.departed pair. After repair, those same public regressions passed **2 tests
in 0.48s** (`gc14-green-confirmed`). A first green assertion incorrectly required
synthetic actors to have mechanical metadata; it was corrected to preserve None
and verify their actual spatial Cell without manufacturing D&D state.

Additional fixture refinements respected existing behavior: earlier unprocessed
perception inputs fenced a later Observation, so the test explicitly completes
those earlier inputs using a negative test judgment; actual combat must occur in
its authored Scene, so the death test returns there through public movement;
uninitialized abstract NPC targets retain their original refusal. No production
capability or perception guard was relaxed for test convenience.

The broad cross-module run covered query/spatial, GC1 doors/effect provenance, all
GC2 navigation tests, GC3, GC4, event stages/traceability, perception conditions/
queue/performance, NPC output/Result/combat handoff, RF4 positions/history-free
rules, migration and real Check effects. It completed **583 passed / 3 failed in
293.05s**. The three failures were original one-event assumptions, corrected as
follows, with no deletion or lowered threshold:

- Destination Evidence retains every old identity/Scene/history/unique-Actor
  assertion and now additionally checks departure type, identity, phase and scope.
- Concurrent action dispatch checks one exact pair, its source command, actual
  Actor and receipt sequence list; dialogue retains one publication.
- Projection/cognition gap testing uses actual event sequences, preserves both
  continuous-prefix assertions, and covers all four explicit test judgments and
  their pending work across a round trip rather than a fixed `end - 1` offset.

The corrected combined run passed **114 tests in 78.31s**, including all **56 new
tests** and both updated old test files. Two existing Lark warnings remain.

The first full-suite attempt found two further legacy assumptions in the
normal/crash restart integration fixture: its final event count was still 11,
where the added NPC source phase correctly makes it 12 without a world-version
change. The isolated reproduction was **2 failed in 13.82s**. Updating those three
count/range assertions and adding the complete event order, NPC identity, command
association, source/destination Scope and phase assertions passed **2 tests in
13.70s**. All original snapshot/RNG/binding/digest/HP/visits/FK/session uniqueness
assertions remain. The already-failing full process, which had imported the old
test snapshot, was interrupted; it is not reported as a complete passing gate.
The next complete run used all files without exclusions:

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=.test-tmp/gc14-full-final tests
```

It completed **1213 passed / 1 failed in 641.49s**. The remaining failure was the
same old 11-event expectation in the original Scene-revisit session test, not a
new production defect. Its count is now 12, and all original flags, objects,
visits, unique Actor location and world-version assertions remain. Additional
checks cover the complete event order, same-command adjacent NPC stages, Actor
identity, Signal source/scope and frozen phase/source Scene. Running the entire
session file after this correction passed **44 tests in 6.64s**, with two existing
Lark warnings:

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=.test-tmp/gc14-sessions-fixed tests/test_sessions.py
```

The final complete gate below ran after this last test-only correction; the
failed complete log is retained as `.test-tmp/gc14-full-final.log`.

## Final local validation gate

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=.test-tmp/gc14-full-complete tests
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
git diff --check
git diff --cached --check
```

Full suite: **1214 passed, 2 warnings in 558.13s (0:09:18)**. All tests were
included, covering the 56 new regressions and original GC1–GC4, navigation,
migration, perception, NPC handoff, real pinned-RE and persistence tests. The two
warnings are the existing Lark imports of deprecated `sre_parse` and
`sre_constants`; no dependency change is made to silence them. The complete log
is `.test-tmp/gc14-full-complete.log`.

Ruff, format and both diff checks passed. The three edited documents' local links
and code fences were also checked. Only the 13 task files are staged; no migration
SQL or dependency files changed. No required local check was skipped. Remote CI
was not executed or claimed as passed.

## Independent second review

A separate read-only reviewer inspected both production changes, all new tests,
the archived worker and both legacy-test updates. It found **no blocking issues**
and confirmed that old checks were preserved and strengthened. It independently
ran:

```powershell
$env:PYTHONUTF8='1'
uv run --locked pytest tests/test_world_query_contract.py tests/test_gc14_review_fixes.py tests/test_gc14_history_and_locks.py tests/test_event_local_evidence.py tests/test_npc_outputs.py --basetemp=.test-tmp/gc14-independent-review -q
```

Result: **114 passed in 67.27s**, with two existing Lark warnings. This was an
independent Agent review and execution, not a second self-review. The reviewer
made no source edits, commit or push and did not replace the full SM gate.

The integration-only incremental fix also received independent read-only review
and execution:

```powershell
uv run --locked pytest tests/test_integration.py --basetemp=.test-tmp/gc14-independent-integration -q
```

Result: **2 passed in 24.91s**, with two existing Lark warnings. The independent
reviewer confirmed all original durable-state and recovery assertions remained
and the new identity/order/scope/phase checks strengthened the regression.

The final Scene-revisit correction also received independent read-only review;
the reviewer found no blocking issues or weakened old assertions and ran:

```powershell
uv run --locked pytest tests/test_sessions.py --basetemp=.test-tmp/gc14-independent-sessions -q
```

Result: **44 passed in 6.91s**, with the same two existing Lark warnings. No
reviewer edits, commits or pushes were made.

## Locked environment and dependency evidence

Both the declared sources and installed distribution `direct_url.json` identify
`dnd5e-engine` and `dnd5e-srd-data` as **0.6.0**, from the required commit
`675552406e67470bf3456fe90e88c4f1595bf324`. The test environment uses these installed
distributions, not another checkout or a local-path substitute.

`git diff --exit-code 359e096 -- pyproject.toml uv.lock` is empty. Git-normalized
working-file blobs also equal the starting baseline blobs:

| File | Baseline and current blob |
|---|---|
| pyproject.toml | `e0fbc7fc884fc803a1850e0e271fb2cf72ceb506` |
| uv.lock | `b78e4eb76bb7b125b0449c540d849126520afb6f` |

## Remaining limitations

Package consequences retain their PC-current-Scene restriction. Several newer
semantic events, including actor.departed, are not Package-configurable triggers.
The path from blocked activity to a fresh NPC cognition decision remains future
work. This repair does not turn blocked into an Observation, broken-lock fact,
Goal change, new Episode or automatic EVA. No real LLM, offscreen-write permission,
dependency upgrade, new gameplay, full Handler split or complete Agentic loop is
claimed. Original GC4 validation is historical; this document records this run.
