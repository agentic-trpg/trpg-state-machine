# MechanismService progress — 2026-10-18

## 2026-10-21 文档目标补充（未实施）

Architecture Decision Status **[DECIDED]**；完整 Session 内存运行层 Implementation Status **[NOT IMPLEMENTED]**；Acceptance Status **本次未执行运行验收**。 当前触发／组件索引每次或批内构建，跨命令复用仍 NOT IMPLEMENTED；目标接入同版本 RAM／候选及统一提交，维护配置／关系／损坏、加载／卸载与恢复依赖。 统一责任与验收引用 [Session Management](SESSION_MANAGEMENT.md#session-内存运行生命周期与事实访问2026-10-21)，沿既有 C-25／C-27／A2／DS 跟踪，不另造任务体系。本轮仅修改文档，未执行本领域业务或性能测试；下述历史日期／SHA／测试数字保留原证据范围。

2026-10-20：能力配置和 effect 来源改取实例记录，触发索引每次从该 Session 的实际实例构建，
不再按 Package digest 跨 Session／候选复用。规则 world-source 端口仍 unsupported。
详见 [实例闭包与验证](INSTANCE_CONFIGURATION_PROGRESS.md)。

## Baseline and preserved work

Feature-delivery branch: `feat/world-mechanism-service`. Implementation and validation run
in `.worktrees/mechanism`, isolated from the 38 pre-existing modified/untracked
Environment/OSS files. Main and starting HEAD were freshly verified at
`9ce60cbe4fdbf91dce6ffef811969482369fb08a`. Architecture main was freshly verified at
`98e8d36b03bdc6b9578082bcdae1ed99ccbc5997`; MODULE_CONTRACTS remains authoritative.
No applicable AGENTS.md, check-fast configuration, Makefile or CI workflow was
found. pytest, Ruff lint/format and diff checks are the available checks.
Existing W1-W8 history and W-02/MECH-01 references are retained. No separate existing
mechanism TODO file was found; this file records the new work without renumbering
older entries. SA-01 is not closed. At the feature-delivery stage, no main merge
was authorized or performed; the later authorization is recorded below.

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


## Authorized main integration — 2026-10-18

The user subsequently explicitly requested merging to main. Pre-merge origin/main
is `9ce60cbe4fdbf91dce6ffef811969482369fb08a`; the sole feature commit is
`fcdf10cd15ab6ba2c47eedbeb4f599002092a72f`. Integration uses a no-ff merge in
`.worktrees/mechanism`, now checked out on main. There are no merge conflicts;
src/tests exactly match the validated feature tree. Only this delivery record
changes beyond that tree. The original worktree retains its branch/HEAD and all
38 existing modified/untracked file bytes. The separate Environment feature and
its incompatible Schema34 are not merged. All previously listed unsupported
capabilities, migration integration requirements and full-suite limits remain.

Merge-tree validation: 73 focused tests passed (2 dependency deprecation warnings),
Ruff check/format and diff checks passed. Prior full-suite limitations remain.

## Environment feature integration — 2026-10-18

The user later authorized main→`feat/scene-environment-service` integration in a
new clean worktree, preserving the original dirty directory. The integration
uses main `afe6871` and ENV `06c7af8`; it does not merge ENV into main. Main's
migration34 is retained unchanged and ENV moves to35. Actual historical ENV34
layouts are recognized under the migration lock, retain ENV data and receive the
missing mechanism34 migration atomically. Main34 mechanism states/adoptions are
preserved. Unknown or partial layouts are refused without writes.

The prior incompatible-lineage restriction above describes the isolated main
delivery at that time; the integrated feature now supplies this compatibility
path. Real archived Schema33/main34/ENV34 saves, both services and rollback are
covered by the 90 passing focused tests. Final combined regression results and
commands are recorded in [Environment implementation record](ENVIRONMENT_SERVICE_PROGRESS.md).

## Mechanism effect interface increment — 2026-10-18

User-authorized branch: `feat/world-mechanism-effects-contract`, local and remote
starting HEAD `f2cd863811475ee9b5571641e58a181a73b1d6ec`. The pre-existing clean
branch worktree was moved from `trpg-state-machine-env-main` into
`.test-tmp/mechanism-effects` to put task writes inside the workspace. Original
Environment/OSS uncommitted files remain in the original worktree, unstaged by
this task. No main merge, dependency upgrade or RE repository change is included.

Current architecture was read and verified at
`98e8d36b03bdc6b9578082bcdae1ed99ccbc5997`. Both uv.lock and the installed Engine's
direct_url.json identify `d596161060555630019163ab301e1d25fd816549`.
The actual /23 request has no rules.effect and requires actor_id. Therefore this
work implements the distinct local `mechanism-effect/1` port, with a production
unsupported adapter and fixtures confined to tests. See
[full contract and RE follow-up](MECHANISM_EFFECTS_CONTRACT.md).

Implemented: compatible mechanism/3 effect catalog; closed attack/save/damage/
status parameters; single/area selection and filters; true object source and
stable original-command activation/consequence correlation; complete current
noncombat snapshot, owner poses/OSS contributions, fixed binding and authoritative
RNG; strict result/source/read-fence/target/Delta/RNG validation; bounded
CharacterFacts adoption; ordered world/rule candidates; rejection/unsupported/
pending-choice semantics; original-command replay/conflict/reopen and rollback.
Rule audit stays within activation records, without a new OB/JEV/LLM route.

Fixed a candidate RNG ordering issue exposed by the new integration test:
ordinary real RE checks must adopt their candidate RNG before direct failed-check
activation as well as automatic hooks. Otherwise a rule effect could read the
pre-check state and later be overwritten by the earlier check transition.
Both successful chaining and whole-command rollback now have public-command tests.

No damage/save formula or status timer was added. Actual RE effect evaluation,
status/Effects lifecycle and source termination, zero-HP/death packets, durable
choice continuation, asynchronous/remote evaluation and combat Episode adoption
remain OPEN or explicitly unsupported. The bounded fixture adopter refuses these
incomplete packets instead of storing partial state.

Validation commands explicitly use this worktree's src in PYTHONPATH, repository
Python, PYTHONUTF8=1 and separate local --basetemp directories:

- Final effect contract: `python -m pytest tests/test_mechanism_effects.py -q
  --basetemp=.test-tmp/contract-final -o faulthandler_timeout=40`: **51 passed**,
  two dependency deprecation warnings, exit 0 (12.70 seconds).
- Earlier combined mechanism lifecycle/legacy/migration/Environment compatibility
  and effect contract run: **85 passed**, two dependency deprecation warnings,
  exit 0 (63.97 seconds). This preceded the additional final effect cases; counts
  are separate runs, not additive unique coverage.
- Ruff check src/tests passed; Ruff format --check src/tests: 171 files unchanged.
  git diff --check passed.
- A restricted lifecycle run stalled on real RE tests and was stopped using its
  exact pytest command/process identity; the successful combined and final runs
  used unrestricted execution. Initial fixture development also corrected test
  directory creation/fixture field errors; those failed attempts are not passes.

Full-suite and final delivery evidence follows below when available. Commit SHA
is reported in the final response, avoiding a self-referential documentation SHA.

### Final verification and delivery

- Full `python -m pytest -q --basetemp=.test-tmp/full-final
  -o faulthandler_timeout=45`: **1647 passed, 1 failed**, exit 1 (721.80 seconds).
  The sole failure was test_versioned_state's Schema8 historical subprocess:
  the task's explicit PYTHONPATH made its old RE environment import current SM,
  producing ImportError for CheckPayload before creating its historical database.
  This is a test invocation environment error, not a mechanism/save failure.
- Removed that process-local PYTHONPATH override; pytest's configured pythonpath
  still loads this worktree's src while the historical child loads its own
  installed source. `python -m pytest tests/test_versioned_state.py -q
  --basetemp=.test-tmp/versioned-clean-env -o faulthandler_timeout=40`:
  **13 passed**, exit 0 (5.08 seconds), including the sole failed full-run case.
  No production/test source change was needed. No single all-green full-suite
  invocation is claimed; the full run and corrected module rerun are separate.
- Public perception smoke: after granting the existing NPC access and processing
  the existing event prefix, two rule consequences produced exactly one additional
  authorized activation OB. Observation content contained no rule_results,
  hp_current or state_delta. Original attempts omitted the grant/prefix; those
  setup failures were corrected before claiming this smoke pass.
- Final Ruff lint and format checks passed (171 source/test files), diff checks
  passed, and document links/fences/JSON examples validated.

Only the mechanism implementation/tests and associated module documentation are
staged for the authorized commit and normal push to this feature branch. Main
and the original worktree's uncommitted feature files are not merged/staged.

## Authorized effects-contract main merge — 2026-10-18

The user subsequently requested merging to main. Latest origin/main was verified
at `f2cd863811475ee9b5571641e58a181a73b1d6ec`; the sole incoming feature commit is
`1c72b6b74b3b4d12cda62ff8dcf98971e0d94e6b`. The clean `.worktrees/mechanism` main
checkout was fast-forwarded to that remote baseline, then the effects-contract
branch was merged with --no-ff. There were no conflicts. Merged src/tests and
pyproject.toml/uv.lock exactly match the previously verified feature tree; only
this delivery record differs beyond it.

Merge-tree validation: mechanism lifecycle/legacy/effect contracts and mechanism/
Environment migration combination **101 passed**, two dependency deprecation
warnings, exit 0 (65.88 seconds), with the merge tree's src explicitly bound.
Ruff check passed; Ruff format --check passed (171 source/test files); staged and
unstaged diff checks passed. SHA256 comparison confirmed all 38 original modified/
untracked feature files unchanged. The earlier full-suite invocation and corrected
historical-module rerun remain separately recorded above; no new full-suite pass
is claimed for this merge.

Normal main push is authorized by the user's merge request. Actual RE effect
evaluation, Effects lifecycle/source termination, zero-HP/death packet adoption,
choice continuation and combat integration remain OPEN/unsupported as documented.


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
