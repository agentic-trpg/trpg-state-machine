<!-- Current entry, 2026-10-27: dated NOT IMPLEMENTED/branch-only notes below
retain their historical scope. Current SM main b9ae704 contains the scoped SessionMemory,
instance-field and passage-index implementation; this prose batch does not close its
remaining recovery/multiplayer limits. Resolve required sibling documents from the
logical parallel workspace. -->

## Project Architecture Authority

2026-10-22 runtime instance ownership: read the paired [MC §2.3a](https://github.com/agentic-trpg/agentic-trpg/blob/docs/runtime-instance-ownership/docs/MODULE_CONTRACTS.md#23a-实例字段关联与局部准入-decided) and the [local inventory and acceptance](docs/reviews/RUNTIME_INSTANCE_OWNERSHIP.md). The paired docs branch is not main; in this task its logical sibling is `.worktrees/runtime-instance-ownership/agentic-trpg`. Follow target-instance fields/associations/indices and affected candidate dependencies; ordinary queries and operations must not replace these with Session/Scene scans or Full Audit. Runtime fields belong to the existing SessionMemory/UnitOfWork publication, with immutable owner replacements, bounded retention, rollback isolation and per-connection recovery invalidation. Check real consumers before claiming completion; do not introduce another Session/transaction authority.

This repository is part of the Agentic TRPG project.

For architecture, responsibility, state-ownership or cross-repository interface changes,
read [the system overview](docs/SYSTEM_OVERVIEW.md) first, then consult the relevant
authoritative project documents in the logical sibling repository:

- `../agentic-trpg/docs/MODULE_CONTRACTS.md`
- `../agentic-trpg/docs/STATE_MACHINE_ARCHITECTURE.md`
- `../agentic-trpg/docs/WORLD_SUBSYSTEM_DESIGN.md`

These documents define the project's architecture, ownership boundaries, invariants, and runtime design principles.

Requirements:

1. Treat `[DECIDED]` architecture rules as binding constraints.
2. Distinguish implemented behavior from proposed or unfinished designs.
3. Follow the documented rules for scoped queries, snapshots, initialization audits, and local validation.
4. Do not introduce full-world or full-session integrity audits into ordinary runtime queries, operations, or commit paths without an explicitly documented architectural exception.
5. Preserve correctness, authorization, transaction integrity, and existing invariants when optimizing validation.
6. If an implementation conflicts with the architecture documents, identify the conflict before proceeding.
7. If the documents cannot be accessed, explicitly report this rather than silently proceeding without them.

## Pre-release save policy

- The project has never shipped a release. Saves and SQLite databases from earlier builds are development artifacts. Cross-version compatibility with those artifacts is out of scope unless the user explicitly asks to preserve specific data.
- For schema changes and refactors, create fresh development databases and verify current-schema creation, saving, reopening, recovery, and rollback. Treat older migration code, documentation, and tests as historical evidence rather than a requirement to support previous development schemas.
- Evaluate legacy-only entry points such as `upgrade_instance_configs` against the current task scope. When an entry point serves only historical development saves, its behavior is outside the acceptance criteria. Verify any current-schema consumers before changing or retiring it.

## Development workflow

- 查看、认领或更新待办时，读 [Linear 项目与 Issue 流程](https://github.com/agentic-trpg/agentic-trpg/blob/3b362cb2f51b63bf6ce8b0fc0dbe64671d5efb45/docs/LINEAR_WORKFLOW.md)。

- Before editing, record cwd, remote, branch, HEAD, origin/main baseline and `git status --short --branch`; inspect relevant source/tests, the top-level domain docs and local Progress. Work on a separate branch. Preserve unrelated changes. Resolve sibling-document paths relative to the logical workspace (parallel worktrees may supply the siblings); report inaccessible required documents.
- Work in the existing directory when switching branches and editing can preserve the user's files. Use a separate worktree if switching or editing would interfere with existing changes, if another task needs the current directory, or if the user asks for isolation. Unrelated untracked files can stay in place. Record why a temporary worktree is needed and remove it when its work is safely saved and the directory is no longer needed.
- Do not merge main, reset, stash, rebase, force push, delete other branches or overwrite other work without explicit authorization. A commit/push request does not authorize a main merge.
- Module Contracts is the highest cross-module authority, MVP Scope owns scope, and domain docs refine them. Record architecture decision status separately from implementation status. Current interfaces and target examples must be checked separately against source and tests; MC §1.6 is a dated index, not permission to assume future versions.
- Default implementation validation uses this repository's actual tools: `uv run --locked ruff check src tests examples`, `uv run --locked ruff format --check src tests examples`, and relevant `uv run --locked pytest -q tests/<affected_file>.py`. Use the existing locked environment and a repository-local temporary directory when appropriate. This repository currently has no RE-style make check-fast/check-plan or unified CI tier script; do not invent them.
- For an automatic focused pytest run, use `uv run --locked python tools/run_affected_tests.py`. Add `--plan` to inspect the scope or `--base <ref>` to include a direct comparison of that ref to HEAD. Without a base, the tool uses local changes, or the latest commit when there are no local changes in scope. It skips untracked `.worktrees/` artifacts and root-level `.txt` files. When the planner reports that full SM scope is required, run the printed pytest command explicitly. Use the plan alongside the task's required public-path and regression checks.
- Expand focused tests for public APIs, current schema, authorization, RNG, shared commit/recovery, cross-domain transactions and other concrete high risks. Include real behavior, failure and rollback evidence. Include migration and compatibility tests when the task explicitly requires them under the pre-release save policy. Avoid unrelated full integration suites for docs-only changes; check links/anchors/fences and `git diff --check` instead. Record command exit codes and distinguish unexecuted checks from passing ones.
- For refactors, inspect direct and indirect Full Audit calls, local admission, SQL row range, enumeration, JSON parsing, Snapshot/model construction, projections, indexes and cache validity across the complete call chain. Follow MC §2.3 and §6.5–§6.6: normal command Finalize must not default to Session-wide audit; first prove equivalent local/persistent protection before replacing existing checks.
- Local checks cover every affected entity, dependency and intermediate candidate state. Preserve authorization, OCC, binding, Schema/Snapshot/RNG validation, transaction/retry integrity and disclosure on cache hits. Legitimate Encounter/full mechanical-closure validation and map-wide consumers remain allowed with an explicit dependency scope.
- For local-query/admission changes, add deterministic scale-growth evidence with target scope held fixed while unrelated scenes/objects/actors/items increase. Count SQL rows, parsing, construction, projections or scans; static findings and elapsed time alone do not prove the required scaling.
- Check corresponding DOC and Progress after implementation; update only evidence-backed capability claims and keep dated history. The current audit and remaining refactors are indexed in [Runtime validation scope review](docs/reviews/RUNTIME_VALIDATION_SCOPE_REVIEW.md).
- When releasing RE and SM together and SM pins a new RE commit from the batch, publish RE first and verify that the exact pinned commit is available from its remote. Once RE lands, refresh its existing baseline code graph while publishing and merging SM. After SM lands, refresh its baseline at the verified integration revision when the shared code index is available. If the two projects use separate index stores, their refreshes may overlap. Use `code-graph-research` to verify the configured checkouts and index scope.
- Complete scoped commit and push when authorized by the task, after reviewing the staged diff; stage named task files only. Never automatically merge main. Report branch, starting/final SHA, executed checks, push result, limitations and follow-up issues. Do not claim GitHub Actions passed from local checks.

## Scoped runtime implementation (2026-10-20)

- Apply the upstream MC §2.3 and §6.5–§6.6 classification using the implemented interfaces and cost boundaries in [the scoped runtime acceptance report](docs/reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md). Keep Full Audit at initialization acceptance, recovery/load, migration or explicit diagnostics; ordinary callers use affected domain admission.
- Schema40 spatial lookup and scene acceptance data are derived. Changes to their source facts, indexes or required-row maintenance must preserve trigger installation, rollback, old-reader isolation and incomplete-instance refusal. Do not write these tables as a second state authority.
- Operation memo values belong to one UnitOfWork mutation stage. New writes and every savepoint rollback must invalidate them; public permission/disclosure checks still run.
- Extend `tests/test_scoped_runtime_validation.py` through real public paths for changes to local admission/reading. Count SQL statements, fetched rows, model construction and preparation separately; include scale growth with fixed target scope and distinguish necessary aggregate JSON work.

## References and runtime reuse (2026-10-21)

- Before design or implementation, read the [project contract](../agentic-trpg/docs/MODULE_CONTRACTS.md), relevant domain docs and [unified references](../agentic-trpg/docs/REFERENCES.md). For rule behavior, consult the relevant original passages of the bound version; small changes do not require every rulebook. For lore, confirm the Package's adopted setting, era and adventure edition.
- Cache, Snapshot, query scope and state persistence follow MC §2.3–§2.4; container weight applies [World §2.1b](../agentic-trpg/docs/WORLD_SUBSYSTEM_DESIGN.md#21b-容器重量派生与跨操作复用). Distinguish official rules, project adaptations, engineering choices, target design and current implementation. Report conflicts and inaccessible/unverified sources; never silently change rules/architecture or label memory/inference as verified. External sources are development references and do not introduce ordinary runtime online rule lookup.
- Historical 2026-10-21 branch evidence (superseded as a merge-status claim by main `d6be5a9`): at source `4341f69cbb560edd9c9c1b1bd907ed6c599c0ed0`, `ContainmentState.weights` is operation-local and `relocate` clears it; cross-operation memory weight caching is a target, not implemented capability. Keep this distinction and the original dated Progress evidence when updating docs.

## Session memory runtime target (2026-10-21)

- Follow [the coordinated MC](https://github.com/agentic-trpg/agentic-trpg/blob/docs/session-memory-runtime/docs/MODULE_CONTRACTS.md) §0.3, §2.3–§2.4 and §6; local responsibility/evidence is in [Session Management](docs/SESSION_MANAGEMENT.md). The paired docs branch is not main; resolve sibling paths against the logical parallel workspace.
- Loaded valid active facts, checks, RE inputs, actor views, Snapshots and derived inputs must use one consistent Session RAM scope. Do not requery/reassemble the same SQL facts merely to confirm memory validity. Candidates and previously supplied fixed inputs must remain isolated; preserve necessary commit-time DB constraints, authorization, OCC, bindings and idempotency.
- Every mutation incrementally persists all affected facts/versions/RNG/evidence/Receipt/Outbox/reliable work, then publishes RAM before normal success/current projections. Committed SQL plus failed RAM publication pauses that Session for reconciliation/rebuild; never continue with stale current memory or report an uncommitted failure. RAM and SQLite are not one ACID transaction.
- Inventory every real write path, including Meta/Task/modes, time, Activity/Effects, mechanism chains and background work. Maintenance/import/migration needs an explicit stop/reload boundary. Loading/cold data/recovery use persisted instances consistently, never Package defaults for existing fields.
- Future implementation validation must jointly inspect SQL, current RAM, old reading references/fixed Snapshots, indexes, caches and restart recovery. Cover rollback/unknown commit/post-commit publication failure/lost response, retries/stale NPC results/concurrent taking, loading/unloading/cold data and deterministic scale counters. Existing operation memo, Schema40, GridMaps and Environment acceptance are not complete RAM-layer evidence. Cross-command weight/mechanism reuse remains NOT IMPLEMENTED until independently proven.
- Preserve domain ownership, current public DTO/Schema/RE pin, Stateless RE and single-player MVP. Concrete data structures, fine-grained versions and APIs remain OPEN; future 5–6-player load is a separate measured target. Docs-only work does not authorize implementation or runtime test claims.

## Source documentation maintenance (2026-10-27)

- Follow [the shared source documentation standard](../agentic-trpg/docs/CODE_DOCUMENTATION.md).
  New modules/classes/functions need proportionate docstrings; changed responsibilities,
  inputs/outputs, side effects or constraints require synchronized explanations. Preserve
  stronger existing constraints and review prose against implementation. Plans belong in
  contracts; current docstrings are the future index's maintained functional explanation.
- Use the applicable repository workflows in `.agents/skills/`: `trpg-runtime-change`, `trpg-locality-review`, `trpg-atomicity-review`, `trpg-code-documentation`.
  Read each matching SKILL.md explicitly when needed; these mandatory requirements do
  not depend on automatic triggering. Resolve sibling DOC paths from the logical workspace
  root. Keep the SM/RE documentation workflow copies consistent with the one shared standard.

## System overview navigation

For changes to system structure, cross-module responsibilities, state ownership or
cross-repository interfaces, first read [this repository's system overview](docs/SYSTEM_OVERVIEW.md),
then the relevant authoritative contracts and current source. Update the overview when
those relationships change. Local function/docstring edits require the relevant source
and necessary context; they do not require rereading all architecture documents or
mechanically changing the overview.

## Optional local code graph

- For call chains, consumers, or cross-module impact, consider the local `trpg-state-machine` graph when available. Check the workspace, index location, and configured checkout against the revision under investigation, then confirm the project with `lct_graph.list_projects`.
- Use `lct_graph.resolve` and `definition` for symbols, and `callers` or `callees` for relationships. Verify consequential results in source. Use local source search for text lookups or when the matching graph is unavailable.
- Follow the `code-graph-research` skill for index maintenance. After code lands on `main`, verify the resulting revision and incrementally refresh this repository's local baseline graph if it exists. Keep candidate indexes separate from the baseline. First-time indexing and full rebuilds require explicit human authorization.
