# SM-10–SM-13 support and migration matrix

Historical implementation record. Current dependency/schema/compatibility and
component ownership are specified in [RF-RE4](RF_RE4_HISTORY_FREE_RULES.md).
The 2026-10-17 Openable pin, verified increments and unfinished boundaries are
recorded in [Openable progress](OPENABLE_SERVICE_PROGRESS.md).

| Capability | Actual status and evidence |
|---|---|
| Single authority before/inside/after combat | Supported: `characters`, `mechanical_actors`, `mechanical_scenes` share facts; no snapshot overwrite. `test_versioned_state`, `test_cross_phase` inspect SQLite. |
| Generic ability/skill check | Supported with complete Snapshot V2 and trusted GM grant; proficiency/expertise and explicit flag advantage/disadvantage are computed by Engine. `test_real_checks`. |
| Basic weapon inputs | Real rapier (including occupied other hand), longsword one/two handed, greatsword two handed tested against Engine events/deltas. |
| Other weapons/mechanics | Dagger's Light/Thrown properties and trained mastery explicitly unsupported in this candidate slice. Grip disagreement is rejected by Engine. Invalid complete equipment facts reject Package loading. |
| NPC attacks | Existing trusted NPC grants and real Bandit actions through the same committer; generic attack Signals only. |
| Victory/TPK closure | Supported with upstream CombatClose, existing final balances and new XP increments; save/new-process resume works. B1 resolved for this bounded slice. |
| RNG continuity | Same persisted stream across checks, combat initialization, PC/NPC attacks, closure and postcombat check. Closure/init/world operations do not reset or consume it. |
| Resource balances | Existing explicit spell-slot/custom-counter balances survive the complete pipeline. No admitted noncombat resource-consuming operation: granted-die/tool paths are unsupported with no RNG/resource change. Resource-consuming cross-phase mechanics remain deferred, not proved. |
| Durable failure handling | Result/schema/config/runtime/storage failures remain independent and retryable; business refusals/conflicts have receipts. Response loss after commit replays without evaluation or payment. |
| Active-combat process interruption | Explicitly unrestorable under MVP; committed attacks are preserved, no forced ending/reset. |
| Further Encounters | One authored encounter per Session in this local foundation; no reset/reinitialization after closure. |
| Missing mechanical Actor/Scene facts | Fail closed; additional scene templates initialize lazily, but do not synthesize missing NPC character mechanics. |
| Full effects/Reaction/Continuation/Saving Throws/spells | Explicitly out of scope. Host, EVA, Memory/Belief, full perception propagation and a production adventure remain separate MVP work. |

## Storage compatibility

Schema 9 adds `characters.xp` and `sessions.mechanical_schema_version` under the
existing serialized, transactional migration. No binding, digest, HP, inventory,
RNG, receipt or event rewriting. No migration beyond 9 was needed for closure or
checks. Phase is derived from `combats.state_json.ended`, never stored separately.

Historical evaluator /4 Sessions remain readable with their original Package and
receipts, but cannot execute new /8 operations. Historical regression uses a
separate locked environment. Snapshot V2 is initialized from approved Package
facts once; the new evaluator does not authorize template reload or save upgrades.

XP is a SQLite integer in the core character row. Fault testing confirmed that
out-of-range accepted increments could otherwise overflow/coerce storage. The
committer now rejects unrepresentable increments as invalid_rule_result and
rolls back normalization/ended/RNG/events/receipt. This is a persistence invariant,
not an XP formula or second reward calculator.

## Reproducible example and evidence

Run `uv run --locked python examples/real_cross_phase.py --directory .test-tmp/demo-new`.
The directory must be new; it retains the original generated Package and SQLite
database for inspection. The script uses real bundled rule assets, a synthetic
original adventure, and launches a fresh interpreter for resume and the next
check. Its trusted context is a demonstration Host capability, not authentication.
The example itself is covered by an automated real-database integration test.

`test_complete_real_cross_phase_pipeline_and_new_process` checks every checkpoint:
world/event versions, RNG, HP/equipment/resource balances, XP, receipt count,
outbox/evidence records, attack Observations, private check details and a second
Session. No live state is seeded with SQL or an overwritten Snapshot.

F1–F5/R1 remain in the full suite. F6 is aligned for the new bounded attack,
closure and check slice; a complete general Rule Evaluation contract is still
partially resolved because unsupported mechanics and formal continuations remain.
General event-only/Meta version policy remains OPEN.

## Batch verification

SM-13 adds 16 test instances: 15 cross-phase/weapon/fault/example cases and a
second actual historical database version. It also hardens the existing closure
and check fault tests: mutated JSON is first parsed into a valid upstream
RuleEvaluationResult so tuple/schema rejection cannot mask the intended semantic
validation. The real-result closure/check/cross-phase group passes 69 tests.

Final full suite: 396 passed using `uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/b13-verified-full`.
Locked sync, Ruff check, Ruff format check and `git diff --check` passed.
All historical tests remain present. Two lark deprecation warnings originate
from the upstream dependency and do not indicate failed checks.
