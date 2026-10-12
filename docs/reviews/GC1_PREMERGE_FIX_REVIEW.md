# GC-1 premerge corrections: shared doors and effect provenance

Repository: `D:\Projects\agentic-trpg\trpg-state-machine`.
Remote: `https://github.com/agentic-trpg/trpg-state-machine.git`.
Branch: `feat/sm-gc1-world-interactions`.
Starting HEAD and reviewed baseline: `8fe53d3be6b26a48742a72274a24b7a662535dd6`.
The starting worktree was clean. The historical GC-1 review is retained separately.

## Reproduction and corrected behavior

Public `submit_command` calls against real SQLite reproduced all three failures
in `tests/test_gc1_doors_and_effects.py` on the unchanged baseline (3 failed,
1.81s). These are complete command/authorization/transaction/event paths, not
isolated `_operate` unit tests:

- F01, door stored in A: A open and traversal to B succeeded; B close returned
  `invalid_target`. Door stored in B: opening from A returned `invalid_target`.
- F02: a declared Package condition triggered a same-Scene remote door effect;
  it returned `object_out_of_reach` and rolled back the original flag change.
- Baseline Fast remained green: 110 passed, 7.91s.

`SetObjectState.scene_id` now denotes the manual interaction Scene. Shared door
authorization uses a pinned Passage endpoint in the actual actor's Scene,
visibility of its local threshold, its exact standing Cell, clear adjacent reach
and supported actor state. Both threshold occupancies are checked before closing.
The same object row/state is used from either side; no coordinate reinterpretation
or cross-Scene object write API is added. The read view and action query express
the same local door, independently of exit direction. Storage location never
implies a single-sided control. No single-sided shared-door configuration is
implemented; unbound switches explicitly remain storage-local and PC-only.

Controlled NPCs share this bounded door admission with PCs. `NPCDoorIntent` uses
the existing selected Task, durable handoff, command identity and reconciliation.
Both trusted control grants and persisted Task-controller grants remain checked.
Invalid switch outputs are rejected, and failed execution has its real receipt;
NPC flags, general switch writes and full Policy are not enabled.

All four existing `_operate` caller paths were audited. `commands.py` supplies the
authorized actor; `events.py`, `dnd_runtime.py` and `mechanical_runtime.py` explicitly
supply private closed origins only after their existing Package/GM/rule admission.
Missing/mixed origins fail closed; no origin is decoded from LLM/client labels.
Automatic effects use the existing current/storage Scene scope, skip character
reach, and retain declared targets, preconditions, switch support, occupied
threshold rejection, combat capability, OCC, savepoint/transaction and idempotency.
Conditional checks retain the existing narrower receipt policy: cascade failures
produce durable rejection receipts, while other effect validation errors roll back
the whole check/RNG transaction without a receipt. This batch does not expand it.

Shared-door manual events freeze the true acting character's local Scene/Cell.
Automatic object events freeze the actual object's state/Cell in its storage Scene
and carry the admitted reference; they do not claim a PC hand action or movement.
No parallel events, Outbox, observation engine or Episode/EVA changes are added.

## Regression and review ledger

Validation uses the installed pinned environment with `PYTHONUTF8=1`, `UV_OFFLINE=1`
and repository-local UV cache. Host authorization is used for subprocess/SQLite
concurrency tests, as in the historical delivery. No failure is skipped and no
threshold is changed.

New regressions cover both storage sides, A/B PC operations and return traversal,
NPC operation while the PC remains in another Scene, revoked grants, remote and
unbound switch rejection, one-way traversal with bilateral controls, both occupied
thresholds, independent crates/doors/walls, original-action rollback, stage-time
evidence, real-RE success/failure consequences, SQL faults, response loss, reopened
SQLite, receipt replay, OCC and spoofed client origin fields. An old multi-blocker
test now approaches/closes before placing blockers, preserving all blocker checks;
new tests explicitly require blocked local door control to fail.

Independent read-only review covered the complete current diff relative to
`8fe53d3` and the entire final GC-1 worktree relative to local main/origin/main
`058e79f300016f7397f0c3401bc03b58554e2b71`, including new files. It found a valid
multiple-port ordering defect: the first blocked local port could hide a second
clear port for the same door. Admission now tests each matching local port,
exempting only that door's corresponding threshold; two ordering regressions and
an independent real-SQLite public-API reproduction confirm the correction while
the original crate and blocked traversal remain intact. Final review reported no
remaining blocking findings, permission bypass or duplicate position authority.
The reviewer's incomplete sandbox test run was stopped and is not counted as
passing evidence; the primary agent's completed host runs are the test evidence.

Final validation evidence:

- Final Fast: 110 passed, 13.76s.
- Final related run: `tests/test_gc1_doors_and_effects.py tests/test_spatial_world.py
  tests/test_events.py tests/test_conditional_world_effects.py tests/test_npc_outputs.py
  tests/test_event_local_evidence.py`: **159 passed**, 84.80s. This includes all
  **73 GC-1 cases** (37 new door/effect cases and the existing 36 spatial cases).
  Case counts were independently checked with `pytest --collect-only`.
- Full run started before the final ordering/test additions: 910 passed, 492.79s.
  This is superseded by the final full run and is not final-code acceptance.
- Final full SM run: `.venv/Scripts/python.exe -m pytest -q
  --basetemp=.test-tmp/gc1-fix-final-full`: **913 passed**, **454.14s**, two existing
  Lark deprecation warnings, no failures or skips. It includes all final code and
  all 37 new regressions. Final edits afterwards only record these results.
- Crate and stool examples: each 16 commands, world version 14, unchanged RNG,
  exact receipt replay and equal fresh-process state including door states.
- `ruff check .`, `ruff format --check .` (131 files), `git diff --check` and local
  documentation-link validation passed. `git diff --exit-code 8fe53d3 --
  pyproject.toml uv.lock` and the same comparison against main/origin/main passed;
  installed Engine/SRD both report `0.6.0`.

Dependencies remain Engine/SRD `0.6.0`, locked revision
`602dcb8d448e670049427bbb751d6ed226005298`; neither `pyproject.toml` nor `uv.lock`
is changed relative to the reviewed baseline or main. This batch retains the
single-Scene coordinate model, transient movement projection, combat position
handoff, unsupported combat dynamic obstacles, ordinary zero time/action-budget
cost and historical-save exclusions. It adds no navigation geography, Activity,
acoustics, dynamic code, complete NPC Policy or cross-Scene automatic effect scope.

See [the current operation/origin contract](../GC1_SPATIAL_WORLD_INTERACTIONS.md).
