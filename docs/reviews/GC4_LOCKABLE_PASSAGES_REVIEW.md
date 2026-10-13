# GC-4 review: lockable passages with owned item credentials

## Baseline and scope

Repository: `D:/Projects/agentic-trpg/trpg-state-machine`, origin
`https://github.com/agentic-trpg/trpg-state-machine.git`.
Existing user branch: `feat/sm-gc4-lockable-passages`.
Clean starting HEAD and fetched origin/main:
`1098bd749eaa2ca114f8d7e36e8058f6bec0089f`, containing required GC3
`80bc1af59993d29f3281e99b06de60915dc450a9`.
No applicable repository/ancestor AGENTS.md was found. No stash/reset/rebase,
dependency upgrade, other repository write, main merge or next batch.

Reviewed GC1/GC2/GC3, RF1, RF_RE4 and RF4 position-repair documentation; audited
WorldHandler, WorldFacts, CharacterFacts, ActivityHandler, command/session control,
Package/Check consequences, NPC Task/Result/handoff and EventCapture. Architecture
repository was read-only. Responsibility and support scope were written before
implementation in [GC4 design](../GC4_LOCKABLE_PASSAGES.md).

Engine/SRD stay 0.6.0 at `675552406e67470bf3456fe90e88c4f1595bf324`.
`pyproject.toml` and `uv.lock` have unchanged Git blob identities against the starting HEAD;
the fixed-loader sources and rules snapshot formats were not edited.

## Implementation and consumer review

WorldFacts owns one independent lock row per shared door and the approved
instance-binding rows. Open/closed remains solely on `objects`; neither endpoint
gets a duplicate locked flag. Schema25 appends tables, FKs, uniqueness and SQL
invariant triggers without rewriting earlier migration SQL. Fresh creation applies
the approved Package /5 content after GC3 initialization. Restore/revisit does not.

The common executor requires an approved portable instance currently carried by
the actual Actor and validates the complete inventory closure. It preserves
quantity, charges, owner, equipment, resources, RNG and game time. Manual controls
reuse actual local ports, visibility, standing/reach, activity and state guards;
Host control cannot supply a missing in-world credential. New no-ops perform the
same admission before reporting success.

All existing ordinary opening and passage consumers were checked: legacy helpers,
NPCDoorIntent, GC2 ordinary opening, WorldFacts.switch, StepActor, EnterScene,
MoveActor, TraversePassage, Package effects and CheckWorldEffects. Closed locked
doors block the original movement projection. Contradictory open/locked state is
rejected, not corrected. Static navigation planning and sensory geometry are
unchanged. Blocked navigation releases qualification through the existing GC2
protocol; explicit unlocking/resume or cancel/new plan is required.

Actual CheckWorldEffects testing found that a new lock refusal initially rolled
back but escaped as an exception without an ordinary receipt. The final change
adds only `door_locked` / `invalid_lock_state` beside the existing cascade refusal
in the real-check savepoint path. The test proves all branch flags, RNG, events,
evidence and Outbox roll back; the refusal receipt replays exactly. Storage and
other prior error semantics remain unchanged. No automatic unlock capability or
Actor impersonation was introduced.

NPC lock outputs have explicit new discriminators in both Result fingerprint data
and pending payloads. Old Door/Pickup/Drop/Navigation JSON is byte-compared with
actual archived GC3 serialization; old typed command replay is verified against
an original Schema24 transfer receipt. Dispatch rechecks current identity,
controller, position and credential; Result selection does not execute actions.

Stage capture freezes actual Actor coordinates at `world.after_door_lock_update`,
including reverse ports. Only one semantic lock event is generated per change.
Controlled audit retains the credential reference; generic Signal content contains
no credential ID or binding catalogue. Refusals generate no physical lock attempt
or new observation/cognition protocol. Historical Evidence is unchanged.

## Executed validation

Baseline Fast: **162 passed in 52.44s**, two existing Lark warnings. It ran from an
exact `git archive 1098bd7` source/test snapshot. The original sandbox run was
interrupted after abnormal CPU/elapsed time; the host rerun used `pwsh`,
`login:false`, workspace basetemp and explicit `GIT_DIR` for historical tests.
An initial archive run omitted GIT_DIR and had 158 passed / 4 archive-path failures;
that setup issue was corrected before the complete passing baseline gate.

The baseline selection was test_package, test_commands, test_spatial_world,
test_navigation_activities, test_history_free_rules_adapter and
test_rf4_position_evidence.

During development, new fixture failures exposed missing third-Scene membership,
asset binding after adding amulet, real diagonal/occupancy barriers, missing NPC
controller registration, Signal field spelling and stale test imports. These
were corrected in fixtures/assertions, preserving the existing gameplay guards.
A local occupied threshold legitimately refused at reach before close-occupancy;
the final public regression puts an actual NPC on the opposite threshold and
checks the intended occupancy gate. A visibility assertion was corrected to
exclude bindings from door data while preserving visible ground-item identities.
No old test was deleted or weakened; legacy schema assertions now expect 25.

The first full gate completed with **1,153 passed / 5 failed in 717.95s**. All five
failures were the parameterized historical perception migration test's table-name
allowlist, which did not yet list Schema25's two additive tables. The final test
adds exactly those two names and separately asserts both tables are empty. Every
original-table equality, pre-upgrade crash rollback, multi-process opener, cursor,
queue, FK and integrity assertion remains. A separate reviewer inspected these
four lines and confirmed that they preserve validation strength and prove old
Sessions receive no new lock capability. No production change was needed.

An intermediate combined GC4/conditional-effects run passed **67 tests in 27.67s**.
The final GC4 set contains **76 new tests** covering two doors and both roles,
same-slug instances, both ports/one-way traversal, current ownership, qualification,
queries, explicit navigation recovery, real automatic effects, real NPC tasks,
concurrent SQLite OCC, technical faults, response loss and actual migrations.

The repaired historical migration group passed **5 tests in 80.48s**. The final
complete gate then passed **1,158 tests in 578.58s**, with only the two existing
Lark warnings. No failures or tests were skipped. The complete set retains all
GC1 two-sided door/automatic-source, GC2, GC3, RF4/history-free rules, position
evidence, NPC handoff, migration and actual pinned RE regressions.

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=.test-tmp/gc4-full-final tests
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
git diff --check
git diff --cached --check
git diff --exit-code 1098bd749eaa2ca114f8d7e36e8058f6bec0089f -- pyproject.toml uv.lock
```

Ruff, format (183 files) and whitespace checks pass. Git blob identities remain
`e0fbc7fc884fc803a1850e0e271fb2cf72ceb506` for pyproject.toml and
`b78e4eb76bb7b125b0449c540d849126520afb6f` for uv.lock, equal to the baseline.
Git's existing Windows line-ending normalization is retained. GC4 Markdown links
and code fences were checked. Logs remain local under `.test-tmp/gc4-full.log`
(initial failure) and `.test-tmp/gc4-full-final.log` (passing final gate).

Direct public example execution also passed: new-process restore reported
`scene.workshop`, `closed`, `locked=true`, world_version 17 and the same owned
amulet plus the unchanged legacy RF4 inventory instance. The example is also
executed by the full suite and independent review.

## Independent review

A separate read-only reviewer inspected the complete diff and all new files,
including the final incremental changes. It independently ran:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_lockable_passages.py tests/test_lockable_boundaries.py tests/test_lockable_migration.py -q --tb=short --basetemp=.test-tmp/gc4-final-independent-v2
```

Result: **76 passed in 74.72s**, two existing Lark deprecation warnings.
Final conclusion: **no blocking findings**. Review specifically checked duplicate
state, bypass consumers, instance vs slug matching, static knowledge, NPC type
selection, activity qualification, half-commits, evidence provenance and old-save
compatibility. The reviewer did not edit, commit or push files. The previously
identified evidence gaps were filled and reviewed before this conclusion.

## Delivery boundaries

This is public deterministic noncombat lock control with explicitly configured
owned credentials. The example uses actual amulets as an original mechanism's
specific tokens without invoking their magic. It is not complete D&D lockpicking,
broken locks, repair, magic opening, breakage, physical simulation, containers,
equipment, arbitrary remote effects, continuous access permission or an LLM plan.

Reference: [Evennia SimpleDoor](https://www.evennia.com/docs/latest/Contribs/Contrib-Simpledoor.html).
Only its two-sided door extension boundary is borrowed; no dependency or
superuser lock bypass is adopted. Local executed results do not establish remote
CI success. Only the requested branch is delivered; no merge or next-batch work.
