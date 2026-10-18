# MechanismService progress — 2026-10-18

## Baseline and preserved work

Current branch: `feat/world-mechanism-service`. Implementation and validation run
in `.worktrees/mechanism`, isolated from the 38 pre-existing modified/untracked
Environment/OSS files. Main and starting HEAD were freshly verified at
`9ce60cbe4fdbf91dce6ffef811969482369fb08a`. Architecture main was freshly verified at
`98e8d36b03bdc6b9578082bcdae1ed99ccbc5997`; MODULE_CONTRACTS remains authoritative.
No applicable AGENTS.md, check-fast configuration, Makefile or CI workflow was
found. pytest, Ruff lint/format and diff checks are the available checks.
Existing W1-W8 history and W-02/MECH-01 references are retained. No separate existing
mechanism TODO file was found; this file records the new work without renumbering
older entries. SA-01 is not closed. No main merge is authorized or performed.

## Implemented and verified scope

- **MECH-01 COMPLETE on this branch:** first reproduced a false unavailable result
  through the real Session query. Ordered local candidates now permit opening A
  before closing B at the same cell. Real collision and directional-jam negative
  cases still refuse; query database rows remain identical. Query/execute share
  the same per-step capability/OSS checks.
- /2 immutable definition, explicit up/down operations, optional bounded uses,
  no-op consumption policy, lifecycle status and exact internal consequence origin.
- Independent activation/consequence identity, one-shot compatibility, replay,
  source references, rollback and persistence across reopen.
- Deterministic lifecycle, explicit owned tool requirements, actual RE ordinary
  ability/skill checks, configured legal-failure activation, permanent disarm,
  temporary block/unblock, prepare, manual reset and WorldTime second deadlines.
- Actual movement/region, object-operation and adopted object-check hooks, indexed
  routing, stable instance order, entry vs dwell, lifetime per-actor frequency,
  after-stage deduplication and shared cascade rollback.
- Portable instance pickup/put/take/drop, prepare, trigger, permanent disarm and
  recover; existing unique location, containment/access and inventory projections.
- Same-transaction admitted component destruction and rollback; this uses the
  existing internal settlement fixture and is not a new RE damage API.

## Verification evidence

Commands use the existing repository Python environment, `PYTHONUTF8=1` and
separate repository-local `--basetemp` paths. Restricted execution stalled on real
rule/subprocess paths, so successful focused checks ran outside that restriction.

- Initial MECH-01 reproduction: 1 failed (legal query), 1 passed (real collision).
- Early mechanism + original mechanism + Openable check combination: 44 passed.
- Extended mechanism regression + original compatibility: 33 passed.
- Affected-component combination (world_*, openable_*, OSS, mechanism/migration,
  commands, conditional effects): 278 passed, 2 dependency deprecation warnings.
- RE/Domain combination (real_checks, domain_runtime, history_free_rules_adapter,
  dnd_runtime), excluding historical/process/migration names: 88 passed,
  2 deselected, 2 dependency deprecation warnings.
- Schema33 mechanism-table preservation/fault/retry focused test: 1 passed.
- Full pytest was attempted and interrupted after faulthandler showed an existing
  historical-runtime subprocess wait in test_dnd_hardening. No full-suite pass is
  claimed. The earlier sandboxed real-check attempt was also interrupted.

Final checks are recorded below. The earlier counts are separate runs and must
not be added as unique test coverage. Delivery SHA is reported in the final chat
response, rather than embedding a self-referential commit ID in this file.

## Compatibility and OPEN boundaries

See [contract](MECHANISM_SERVICE.md) for exact selector/state/identity semantics,
Schema33→34 preservation and unsupported capabilities by owner. Package /10
reserves /9 for the original uncommitted Environment work. This branch's Schema34
and that Environment Schema34 are different lineages: incompatible saves refuse
explicitly. Integration/renumbering remains Storage/Environment work; the original
files were not overwritten or staged.

Damage/save/condition/Effects, per-Turn limits/reset, combat admission/cost/Reaction,
materials, tool proficiency, generic timed Activity servicing, complex pressure or
weight facts, repair and unsupported carried/rigid geometry remain OPEN. No field
or synthetic result is presented as implemented end-to-end support. Existing
combat restrictions and v0.4 Turn/Round cognition boundaries remain unchanged.


## Final validation and delivery arrangement

- Final affected-component combination, explicitly bound to isolated src: 281 passed,
  2 dependency deprecation warnings, exit code 0 (96.48 seconds).
- Final targeted mechanism/migration/legacy/Combat guard/GC14 combination: 73 passed.
- Final movement/navigation/item/evidence/trace combination: 184 passed,
  17 explicitly deselected historical/process/migration cases.
- Final RE/domain combination with isolated src also bound in child processes:
  88 passed, 2 explicitly deselected cases.
- `examples/world_station.py`, explicitly bound to this isolated src using
  PYTHONPATH, completed fresh-process restoration and receipt replay; final
  workshop (1,0), world version 32, event sequence 49.
- Ruff check: passed. Ruff format --check: 233 files already formatted.
  git diff --check and new-document local links/fences: passed.
- Original 38 modified/untracked files: SHA256 hashes unchanged.

Implementation is committed in the detached isolation checkout and pushed to
`origin/feat/world-mechanism-service` without force or main merge. The original
worktree's local branch/HEAD stays at the starting commit so its Environment/OSS
changes retain their original diff base. Use `.worktrees/mechanism` for the
verified runnable result; original worktree files are deliberately not overlaid
with this branch's incompatible pending migration.
