# SM-10: versioned authoritative mechanical state

The fixed Engine/SRD candidate is `602dcb8d448e670049427bbb751d6ed226005298`.
Its actual evaluator is `dnd5e-evaluation/8`: Snapshot V2 uses evaluation /4;
the Engine still accepts the older basic Snapshot V1 subset. This does not
authorize changing a stored Session binding. Historical /4 Sessions retain
their Package digest, binding, state and receipts; new evaluation with an
uninstalled binding fails independently, without a rejection receipt.

## Unique field ownership

| Facts | SQLite owner |
|---|---|
| HP, inventory, XP | `characters` (no copies in mechanical JSON) |
| Ability scores, proficiencies, equipment grip and remaining mechanical facts | `mechanical_actors` |
| Current scene mechanical facts | `mechanical_scenes`, initialized once on first entry |
| Encounter component only | `combats` |
| RNG stream/state/version | `rng_states`, separately from WorldState |
| Binding, Package digest, world/event versions and snapshot schema | `sessions` |

The local `noncombat_foundation` Package extension contains an upstream typed
NonCombatSnapshot V2, initial RNG, explicit initial XP by Actor and optional
additional SceneState templates. It is an authored initialization format,
not another public Rule Engine ABI. Nonempty effects and incomplete mechanical
scenes are explicitly unsupported. All initial writes share creation's atomic
transaction. Runtime snapshots are rebuilt from committed rows before the first
Encounter; combat initialization reuses these facts and never resets RNG.

Schema 9 adds XP (historical default zero) and nullable snapshot-version metadata.
Historical NULL denotes the existing Snapshot V1 boundary; it does not upgrade
rules or rewrite existing facts. Migration remains serialized with the existing
SQLite transaction and is tested for concurrency, interruption and retry.

## Executed evidence

* Unmodified main with its old pinned environment: 313 tests passed.
* Candidate upstream contracts, review findings, attacks, closure, weapons and
  checks: 685 tests passed (real candidate source, separate from SM tests).
* Candidate [CI run](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38050625923):
  Fast / Focused Validation succeeded; Full Validation was skipped.
* SM-10 full suite: 326 passed, two upstream lark deprecation warnings.
  `uv sync --locked --group dev`; `uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/b10-full-final`.
* Ruff check, Ruff format check and `git diff --check` passed.

The historical migration tests construct real databases in a separately locked
old environment exported from commit `a00854374a38b01a13cc0fbfa089ef6e0b709cef`.
They require that Git object and the locked dependency artifacts to be available.
No Rule Engine or Meta checkout was modified. Closure and noncombat execution
remain subsequent batches; this batch establishes their authoritative input.
