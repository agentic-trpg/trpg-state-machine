# SM-11: Engine-proposed combat closure

`TypedCommand(command_type="combat.close", payload=CombatClosePayload(kind="combat.close"), ...)`
uses the existing authenticated command/receipt path, evaluation outside the
write transaction and whole-session read/RNG fence. NPC callers need the same
trusted control grant as attacks. Async submission supports the same operation.

The Engine decides victory/TPK and XP. The bounded SM validator accepts exactly
one CombatClose, its CombatEnded event and the necessary normalization of already
recorded NPC deaths. It checks combat identity, the ended fence, death history,
final HP/temp HP/resource balances and surviving-party XP ownership. It does
not recompute outcome or XP. Unsupported nonempty loot cannot pass the upstream
closed schema. Wrong results are independent `invalid_rule_result` failures.

Only ended, authorized death normalization and new XP increments are written.
Existing balances are informational, never applied twice. Phase is derived from
the single combat component, not independently stored. Delta, XP, RNG, version,
events, receipt and outbox commit atomically. A repeated command replays its
receipt; a new close after closure receives the Engine's combat_ended rejection.

After closure, save/resume and ordinary world operations are available. Tests
resume in an actual new Python interpreter. Interrupted active combat remains
explicitly unrestorable: no fabricated terminal state or reinitialization.

18 new tests cover real victory/TPK, exact final balances, XP, RNG preservation,
replay, malformed closure proposals, SQL faults, version conflict, authorization
and fresh-process recovery. Full regression: 344 passed, two upstream warnings,
using `uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/b11-full`.
Locked sync, Ruff check/format and `git diff --check` passed.
This closes B1 for the admitted terminal slice; complex
outcomes and exact active-combat continuation remain outside scope.
