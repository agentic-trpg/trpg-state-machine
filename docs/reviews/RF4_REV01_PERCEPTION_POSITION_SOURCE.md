# RF4-REV-01: event perception position source repair

## Scope and baseline

Repository: `D:/Projects/agentic-trpg/trpg-state-machine`, origin
`https://github.com/agentic-trpg/trpg-state-machine.git`.
Branch: `fix/sm-rf4-perception-position-source`.
Actual starting HEAD: `4e4e678e1b2e580959fc22d07465940211c66a3a`, with a clean
working tree. This includes GC2 `5ff7a807d5b29cceb4311dcb2fe5b1182ead6bd1`.
No repository/ancestor AGENTS.md was present. RF-1, RF-RE4, GC2 and the current
producer, persistence, perception, spatial and rules adapter code were inspected.

Only SM changes. Engine/SRD remain remotely pinned to
`675552406e67470bf3456fe90e88c4f1595bf324`; rules data and algorithms are unchanged.
Storage remains Schema 23; Evidence remains `/4` and `/5` with unchanged shapes.
RE and the architecture repository are read-only. No merge/rebase/force push.

## Root cause and public reproduction

RF4 correctly removed active `actor_zone` from persisted `combats.state_json`.
`EventCapture._event_perception_evidence` nevertheless selected actual
`CharacterFacts.position` only when `WorldFacts.spatial_scene` found
`spatial_enabled=1`; otherwise it read that removed active copy. A full mechanical
Grid can exist with spatial admission disabled. In that case reliable `actors.cell`
was wrongly captured as missing, for both observer and source roles.

The regression uses the unchanged `modern_data(..., spatial=False)`, validates a
complete `/17` Package, and calls public create/initialize APIs with the real pinned
RE and SQLite. SQL confirms actual NPC cell `1,0`, disabled spatial admission,
and no persisted active `actor_zone`; `read_combat_snapshot` correctly projects
the actual cells. A public `SetSceneFlag` triggers a once-per-Session Package
event whose effect only resets the same flag and whose visual Signal has an
approved NPC origin. Its producer phase is `package.after_effect`, captured.
It commits successfully before the fix, but its persisted mechanical cell is None.

The source-role reproduction uses public precombat `EnterScene`: the departure
phase freezes the NPC observer at `1,0` and dynamic PC source at `0,0`. Before the
fix both mechanical cells are None. Package-authored origins cannot name a
Session-generated PC; active combat refuses world movement. The source case
therefore uses the genuinely reachable precombat producer, rather than changing
these permissions or using an unavailable Engine intermediate phase. The active
Package case also exercises an NPC shared between observer and source roles.
Both reproductions retain spatial-enabled controls and cover Evidence `/4`/`/5`.

The initial production-unchanged run was **2 failed / 2 passed in 13.41 s**
(24.67 s outer). Failures were actual persisted-cell comparisons, not expression
or mocked-evaluator tests. A focused source diagnostic additionally showed
`(None, None) != ("1,0", "0,0")` in 4.63 s. After the production fix the original
four cases passed in 42.62 s (45.29 s outer).

Missing coordinates do not universally block judgment: the existing plain bright
Scene branch may ask a general noticing question. The defect is losing already
known positions, not a claim that every missing position forbids perception.

## Repair and ownership

At the actual producer stage and inside the existing transaction, EventCapture:

1. Reads current scoped `mechanical_scenes` with matching stored Scene identity.
2. Requires every typed `MechanicalGrid` field before constructing the upstream
   `GridScene`/`GridTopology`. Movement-only projections cannot acquire sensory
   defaults such as invented bright lighting or absent walls.
3. Reads each existing actor's actual position through CharacterFacts, validates
   matching Scene, canonical coordinates, upstream bounds and legal occupancy.
4. Freezes that same value into observer/source `mechanical.cell`, and into `/5`
   actor `cell`. `/4` retains its original smaller field set.

Missing position/geometry remains None. Missing, incomplete or contradictory
Scene geometry remains unknown. Invalid known coordinates reject the command
through the existing domain-refusal savepoint/receipt protocol. No template,
closed-combat position, cache or callback supplies a replacement. The existing
visual admission retains its bright/plain noticing exception; nontrivial
geometry cannot be supplied by judgment. An explicit Signal origin contradicting
the actual cell remains Unresolved with judgment disabled.

No active position copy is restored to combats. EventPersistence still assigns
identities and atomically writes frozen Evidence, events, Outbox and NPC proofs;
it does not resample the world. No asynchronous historical reader is changed.

## Consumer audit

Searches covered `actor_zone`, `$.actor_zone`, `state.get`, JSON extraction,
current/frozen fallbacks, source roles and both Evidence schemas throughout
production Python/SQL and relevant fixtures. Findings:

| Consumer | Position responsibility | Outcome |
|---|---|---|
| CharacterFacts.position; WorldHandler current-cell/movement readers; GC2 planning/execution | Actual `actors` position and current geometry | Already current; preserved |
| EventCapture._event_perception_evidence, observers and source_actors | Current facts at each actual world/Package producer stage | Sole stale active-copy consumer; fixed together |
| RulesAdapter._prepare_combat and active _combat_snapshot; initialization/OCC guards | Current CharacterFacts projected into RE Snapshot actor_zone | Already current; preserved |
| RulesAdapter close application -> CombatRecords.freeze_positions | Freeze validated actual cells atomically at legal closure | Preserved; no live position authority |
| RulesAdapter._combat_snapshot for ended combat | Closed encounter's frozen historical positions | Preserved; never used by new-event capture |
| service creation foundation actor_zone read | Approved explicit initial cells, only missing fresh-session positions | Preserved; not a current/history fallback |
| signal_environment, judge_signal, project_event/project_npc_event | Frozen Event Evidence only; /4 mechanical cell, /5 top-level cell | Preserved; new fields agree; no current-position query during history projection |
| EventPersistence and queue/barrier proof construction | Frozen staged Evidence and durable historical proofs | Preserved; no position recovery |
| Old save audit/migration and archived runtime tests | Original historical representation under original pins | Preserved; no rewrite or fabricated upgrade |

There is no remaining production `state.get("actor_zone", ...)` or SQL
`json_extract(...actor_zone...)` current-position fallback. The retained JSON
`json_set` is the legal closure freeze writer. RE Snapshot actor_zone remains a
valid computation projection. Current scene-view reads are explicitly current
views, not historical recovery; they do not use combat actor_zone.

## Regression coverage and historical limits

The real SQLite cases cover both spatial modes, both actor roles, malformed,
noncanonical, out-of-bounds and blocked occupancy, missing position, absent or
wrong-identity Scene facts, movement-only geometry, and origin mismatch.
Nontrivial sensory cases use the supported noncombat foundation: current bounded
combat initialization admits only a plain bright Grid. Negative current-fact
corruption is explicitly injected after valid public creation; it is not a
success-path substitute or a relaxed gameplay capability.

Real attack/closure tests freeze terminal positions, move through admitted public world
operations after close, reopen SQLite, and compare original Evidence byte for
byte. A later world event uses the actual new source cell (or explicit unknown
after a nonspatial arrival), never the closed encounter's old cell. Historical
projection uses the earlier alive observer and earlier position even after death,
closure and movement. A forbidden current-position accessor plus SQL trace prove
that asynchronous projection does not query current mechanical/position facts.

SQLite Evidence and Outbox insert faults roll back all facts/events/queue/RNG/
versions/receipts. Same-command retry commits once, and exact replay leaves all
persisted rows unchanged. Domain invalid-position refusal retains only its normal
refusal receipt and similarly replays without mutation.

An actual archived baseline process (`git archive 4e4e678... src`, importing that
source with the same real pinned Engine) creates a database with missing position
Evidence through public APIs. Current opening preserves every old row, original
receipt and queue proof; projection without judgment remains unresolved and the
Evidence stays byte-identical. Reliable current position does not repair the
past. Existing bright/plain general judgment semantics remain applicable, but
cannot restore an old coordinate or prove the missing event-time geometry.
There is no backfill, automatic correction or silent historical upgrade.

## Validation and review

Development acceptance fixtures first attempted nontrivial combat foundations
and an incorrect StepActor command name; those were rejected by existing
capability/schema guards. Fixtures were corrected to admitted noncombat geometry
and `world.step_actor`. Invalid-position assertions were corrected to the public
domain-refusal receipt contract (savepoint rollback plus refusal receipt), keeping
exact equality of every other table and explicit idempotence checks. No existing
assertion was weakened or removed.

The focused real regression command was:

```powershell
uv run --locked python -m pytest -q --tb=short tests/test_rf4_position_evidence.py tests/test_event_local_evidence.py tests/test_perception_conditions.py tests/test_history_free_rules_adapter.py tests/test_spatial_world.py tests/test_npc_combat_handoff.py tests/test_combat_closure.py tests/test_navigation_activities.py tests/test_navigation_faults.py tests/test_navigation_boundaries.py tests/test_navigation_migration.py
```

It passed **244 tests in 77.63 s** (80.37 s outer), with two existing Lark
warnings. Afterward the two insert triggers were narrowed to the actual sensory
Package event, allowing earlier event rows to be written before injection and
rolled back; the final full gate includes that stronger fault scenario.

Final commands, after all source/test edits:

```powershell
uv run --locked python -m pytest -q --tb=short
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
git diff --exit-code 4e4e678e1b2e580959fc22d07465940211c66a3a -- pyproject.toml uv.lock
```

The full suite passed **1,023 tests in 457.41 s** (461.01 s outer), including all
**23** new real SQLite cases and the unchanged fixed-remote Engine/SRD source
assertion. Ruff check passed; **165 files** passed format checking; whitespace
and unchanged-pin checks passed (1.92 s combined). The narrowed sensory-event
fault cases separately passed **2 tests in 1.58 s** before the full gate.

Development fixture runs were 13 failed / 10 passed in 7.62 s, then 8 failed /
15 passed in 9.22 s while correcting the admitted foundation, operation name
and refusal receipt assertions described above. These are not passing gates.
All were resolved before the 244-test focused and 1,023-test full gates.
Two Lark `sre_parse`/`sre_constants` deprecation warnings remain unchanged.
No tests were deleted, existing assertions weakened or coverage thresholds changed.
Logs: `%TEMP%/rf4-rev01-red.log`, `rf4-rev01-green.log`,
`rf4-rev01-focused.log` and `rf4-rev01-full.log`.

Self-review found no remaining blocker in this repair's scope. The containing
commit is the delivery commit; its SHA and remote match are reported after push.
Only the requested repair branch is pushed; GC2/RF4 commits remain ancestors.

This repair uses self-review and executed tests; no new independent agent review
is claimed. No GitHub Actions workflow exists in this repository; local test
success is not a remote CI result.

## Engine-stage interface gap

Engine events still use explicit `engine.intermediate_phase_unavailable` Evidence.
A future batch needs a typed per-event intermediate state/position/condition and
geometry capture contract, deterministic phase ordering and identities, and
alignment with SM's commit/dependency fences. The final Snapshot cannot prove
all preceding event stages. This repair adds none of that protocol, Episode/OB
redesign, LLM calls, full-history input or additional gameplay capability.
