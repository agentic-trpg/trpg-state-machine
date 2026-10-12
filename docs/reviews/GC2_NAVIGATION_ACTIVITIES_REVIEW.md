# GC-2 implementation review

Repository: `agentic-trpg/trpg-state-machine`, working branch
`feat/sm-gc2-navigation-activities`. Clean starting HEAD and fetched `origin/main`:
`9ea750bab2c1ff6d990a02b13b10ffff1c81ed88`, containing required RF-1
`203b704270cda44fe136b6656bec25142f24642e`. Read-only architecture reference:
`8141402592946942ff5f6a69f04858ef382bd734`. No other repository is modified;
no merge, reset, rebase, stash, force-push or dependency upgrade is included.

The [GC-2 design](../GC2_NAVIGATION_ACTIVITIES.md) records the local API, data
owners, planning/knowledge boundary, execution qualification, transaction and
retry rules, NPC handoff, Schedule arbitration and remaining scope.

## Delivered behavior

One authorized high-level Scene/Cell destination creates durable progress.
ActivityHandler selects ordinary movement, shared-door opening and passage
traversal using WorldHandler, then completes or records a bounded stop. A
generic driver commits at most 64 individually receipted advances per batch.
Explicit resume is required after a blocked result. New-process execution
reconciles the original receipts and continues at the persisted cursor.

ActivityRecords owns the plan/cursor/result, never actual position, maps, HP or
RNG. CharacterFacts and WorldFacts retain authority over facts; CommandRuntime
retains the single atomic settlement/receipt chain. No activity component takes
StateMachine or reverse-imports the facade. NPC navigation uses only the new
restricted Result start intent; old move/door/combat intents keep their meaning.

Plans depend on granted immutable Package grids and explicit directed passages,
not remote dynamic SQL world state. Two worlds differing only in an unknown
remote obstacle get equal plans/early steps and diverge only at execution.
Program refusals produce bounded results without invented sensory Signals or
Observations; actual physical events retain their existing stage evidence.

## Executed evidence

Commands use `.venv/Scripts/python.exe -m pytest -q`, `PYTHONUTF8=1`, actual
SQLite files and the existing pinned RE. Subprocess/concurrency/real-RE runs use
the authorized host environment and distinct workspace basetemps. No core
transaction is mocked and no failure is skipped or threshold reduced.

| Stage | Result |
|---|---|
| Before implementation: Fast (package, commands, sessions) | 110 passed, 7.31 s |
| Early navigation + RF-1 + Package + GC-1 after fixture corrections | 107 passed, 10.66 s |
| Navigation/faults/real RF-1 migration before final review additions | 34 passed, 16.41 s |
| Independent-review fix + navigation boundary / RF-1 components | 17 passed, 2.11 s |
| First comprehensive related regression | 454 passed / 5 failed, 245.21 s; strict table-set assertions needed the additive Schema 22 table |
| GC-2 + migration/perception corrections | 74 passed / 1 failed, 66.37 s; abstract fixture reached a pre-navigation capability refusal |
| Corrected configuration/token/plan-limit boundary suite | 10 passed, 1.18 s |
| Final SM complete acceptance, after every correction | 972 passed, 478.91 s; no skips |
| Final examples: NPC/workshop and PC/platform, including fresh-process continuation | Both completed; original NPC batch retried after completion without another move |
| Ruff, format, git diff --check | Passed; 160 Python files formatted |

The comprehensive targets are package, commands, sessions, all four
`test_navigation_*.py` files, domain_runtime, spatial_world,
gc1_doors_and_effects, conditional_world_effects, game_time, npc_results,
npc_outputs, npc_combat_handoff, npc_perception_queue, perception_efficiency,
migrations and real_checks. Final complete acceptance uses
`--basetemp=.test-tmp/gc2-full-final`; it includes every SM test without filtering.
Focused corrections use `--basetemp=.test-tmp/gc2-final-corrections`; the corrected
boundary run uses `--basetemp=.test-tmp/gc2-boundaries-final`.
Static commands are `.venv/Scripts/ruff.exe check .`,
`.venv/Scripts/ruff.exe format --check .` and `git diff --check`.

The final full run includes all 52 new navigation cases and the RF-1,
GC-1, conditional-effects, NPC handoff, perception, historical migration and
real-RE tests. The earlier five comprehensive migration failures are all covered
and pass in this final unfiltered run. Its actual result supersedes intermediate
results above; RF-1's historical 920-pass Review remains a historical record.

Final executed examples (using the same Python executable):

```powershell
python examples/navigation_activity.py --directory .test-tmp/gc2-example-final-npc
python examples/navigation_activity.py --directory .test-tmp/gc2-example-final-pc --actor pc --destination platform
```

Both launch a fresh process internally. NPC/workshop finishes at `(4,2)` with
cursor 6; PC/platform finishes at `(0,4)` with cursor 4. RNG remains unchanged.
An additional `--resume <original-session-id>` on the completed NPC example
reconciles the original batches without another move. Traces remain in each
example directory's `run.json`, `trace.json` and actual SQLite database.

Two existing Lark deprecation warnings (`sre_parse`, `sre_constants`) remain.
Intermediate failures were corrected, not excluded: a success fixture's diagonal
route correctly met a crate, zero-distance evidence included session creation,
one query used a wrong signature, a real-RE fixture needed its actual template
grants, and latest-schema assertions needed 22. The table-set migration assertion
now explicitly requires the new table to be empty while comparing every original
row/column exactly; a first correction used the wrong empty collection type and
was corrected. A configuration refusal fixture was adjusted to retain basic
Package reachability so that it exercises the actual navigation boundary.
An initial restricted run could not create pytest's default Temp directory;
subsequent runs use writable workspace basetemps. One full run was interrupted
after the comprehensive result exposed the stale migration assertions; it is
not counted as acceptance evidence.

## Regression coverage

Public command/real SQLite cases cover multiple actors/destinations, ordinary
doors stored on either side, two-sided return journeys, one-way exits, unrelated
objects remaining intact, zero-distance completion, mid-route obstruction and
explicit resume, ungranted/secret connections, and detached authorized queries.

Idempotent start/advance/cancel, same-key changed arguments, stale world/activity
versions, same/different-command concurrent advances and cancellation races
compare actual position, cursor, versions and RNG. Controller revocation,
replacement, Schedule conflict, HP/position change and actual RE combat all
prevent further old execution. NPC lifecycle claim/release changes its runtime
version only at those boundaries and does not mutate Goal/Plan/Memory.

Real SQLite triggers fail activity writes and Outbox writes at PC/NPC start,
step, door and cancel phases. Every stored table is compared before/after;
there is no partial marker, movement, open door, cursor, event/evidence, version,
RNG or receipt. Removing the fault and reusing the same command succeeds once.
Package tail refusal retains only the existing rejection receipt, rolling back
world/progress/events. Lost-response tests query original receipts. Fresh Python
processes repeat the original batch without moving again, then complete and
retain RNG continuity. A real Task/Result/handoff starts once without an LLM.

Standalone component tests require no StateMachine. They prove opaque issuer,
exact primitive and UnitOfWork binding; an expired qualification cannot relax
busy checks. RF-1's AST ownership/dependency and actual Store scope tests remain.

## Independent review

An independent read-only reviewer inspected the complete baseline diff and new
files for competing authority, permission bypass, omniscient planning, split
position/cursor commits, unbounded execution, Schedule overlap, restart replay,
false OB and facade coupling. It reproduced one P2 through a validated Package,
public create/start commands and real SQLite:

An authorized irrelevant 4097-leg candidate caused immediate `plan_limit`,
rejecting an otherwise available three-leg local route still in the heap.
The planner now prunes only that candidate and continues searching. It reports
the limit only after no bounded solution remains. Public regressions prove both
the short route succeeds and a genuinely overlong target still fails. The
reviewer independently confirmed the fix.

Independent executed checks: 14 navigation/RF-1 boundary cases, 10 navigation
configuration/token/plan-limit cases, and 22 navigation-fault/migration cases.
That last independent run omitted its real-RE combat parameter due restricted
execution; the implementing agent's final host suite includes it. A further
targeted run passed all eight newly added NPC SQL/Outbox fault cases, leaving
the PC parameters to the implementation/full suite. These 54 independent passes
are separate from implementation/full-suite results. The
reviewer also verified historical SQL and dependency files are unchanged.
Final complete-file/document review: **no remaining blocking findings**. This
review conclusion does not substitute for the host full-suite result above.

## Dependencies, history and limits

`git diff 9ea750b -- pyproject.toml uv.lock` is empty. Current Git blob hashes
equal the baseline:

- pyproject.toml: `9c2c2209812fdf6a987ca56294abf2dd1b491777`
- uv.lock: `3a97f45be12dfb595251b48f8109a33ed3668003`

RE and SRD remain 0.6.0 at
`602dcb8d448e670049427bbb751d6ed226005298`. Schema 22 is one additive activity
table/index; `_schema.sql` and migrations 2–21 remain unchanged. The real RF-1
archive migration preserves original rows, Package digest, command fingerprint
and historical receipt. History-based tests still require the pinned Git
commits, and active-combat continuation is still unsupported.

This is one bounded navigation activity. Automatic time costs, exploration or
questions for directions, locks/breaking/moving obstacles, equipment acquisition,
automatic global detours, complete Activity/Goal policy, production Host/model
integration, acoustics and combat obstacles remain outside this batch. Pump
recovery reuses the same request ID and budget; it is not a separate durable task
platform. Revoked NPC execution retains history/reservation until an authorized
cancel/new request. No performance benchmark, whole RE conversion or frozen
cross-repository ABI is claimed.
