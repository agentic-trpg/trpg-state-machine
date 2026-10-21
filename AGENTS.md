## Project Architecture Authority

This repository is part of the Agentic TRPG project.

Before analyzing, designing, or modifying code, read the authoritative project documents located in the sibling repository:

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

## Development workflow

- Before editing, record cwd, remote, branch, HEAD, origin/main baseline and `git status --short --branch`; inspect relevant source/tests, the top-level domain docs and local Progress. Work on a separate branch. Preserve unrelated changes, using an independent worktree when the current checkout is dirty. Resolve sibling-document paths relative to the logical workspace (parallel worktrees may supply the siblings); report inaccessible required documents.
- Do not merge main, reset, stash, rebase, force push, delete other branches or overwrite other work without explicit authorization. A commit/push request does not authorize a main merge.
- Module Contracts is the highest cross-module authority, MVP Scope owns scope, and domain docs refine them. Record architecture decision status separately from implementation status. Current interfaces and target examples must be checked separately against source and tests; MC §1.6 is a dated index, not permission to assume future versions.
- Default implementation validation uses this repository's actual tools: `uv run --locked ruff check src tests examples`, `uv run --locked ruff format --check src tests examples`, and relevant `uv run --locked pytest -q tests/<affected_file>.py`. Use the existing locked environment and a repository-local temporary directory when appropriate. This repository currently has no RE-style make check-fast/check-plan or unified CI tier script; do not invent them.
- Expand focused tests for public APIs, schema/migrations, authorization, RNG, shared commit/recovery, cross-domain transactions and other concrete high risks. Include real behavior, failure/rollback and compatibility evidence. Avoid unrelated full integration suites for docs-only changes; check links/anchors/fences and `git diff --check` instead. Record command exit codes and distinguish unexecuted checks from passing ones.
- For refactors, inspect direct and indirect Full Audit calls, local admission, SQL row range, enumeration, JSON parsing, Snapshot/model construction, projections, indexes and cache validity across the complete call chain. Follow MC §2.3 and §6.5–§6.6: normal command Finalize must not default to Session-wide audit; first prove equivalent local/persistent protection before replacing existing checks.
- Local checks cover every affected entity, dependency and intermediate candidate state. Preserve authorization, OCC, binding, Schema/Snapshot/RNG validation, transaction/retry integrity and disclosure on cache hits. Legitimate Encounter/full mechanical-closure validation and map-wide consumers remain allowed with an explicit dependency scope.
- For local-query/admission changes, add deterministic scale-growth evidence with target scope held fixed while unrelated scenes/objects/actors/items increase. Count SQL rows, parsing, construction, projections or scans; static findings and elapsed time alone do not prove the required scaling.
- Check corresponding DOC and Progress after implementation; update only evidence-backed capability claims and keep dated history. The current audit and remaining refactors are indexed in [Runtime validation scope review](docs/reviews/RUNTIME_VALIDATION_SCOPE_REVIEW.md).
- Complete scoped commit and push when authorized by the task, after reviewing the staged diff; stage named task files only. Never automatically merge main. Report branch, starting/final SHA, executed checks, push result, limitations and follow-up issues. Do not claim GitHub Actions passed from local checks.

## Scoped runtime implementation (2026-10-20)

- Apply the upstream MC §2.3 and §6.5–§6.6 classification using the implemented interfaces and cost boundaries in [the scoped runtime acceptance report](docs/reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md). Keep Full Audit at initialization acceptance, recovery/load, migration or explicit diagnostics; ordinary callers use affected domain admission.
- Schema40 spatial lookup and scene acceptance data are derived. Changes to their source facts, indexes or required-row maintenance must preserve trigger installation, rollback, old-reader isolation and incomplete-instance refusal. Do not write these tables as a second state authority.
- Operation memo values belong to one UnitOfWork mutation stage. New writes and every savepoint rollback must invalidate them; public permission/disclosure checks still run.
- Extend `tests/test_scoped_runtime_validation.py` through real public paths for changes to local admission/reading. Count SQL statements, fetched rows, model construction and preparation separately; include scale growth with fixed target scope and distinguish necessary aggregate JSON work.

## References and runtime reuse (2026-10-21)

- Before design or implementation, read the [project contract](../agentic-trpg/docs/MODULE_CONTRACTS.md), relevant domain docs and [unified references](../agentic-trpg/docs/REFERENCES.md). For rule behavior, consult the relevant original passages of the bound version; small changes do not require every rulebook. For lore, confirm the Package's adopted setting, era and adventure edition.
- Cache, Snapshot, query scope and state persistence follow MC §2.3–§2.4; container weight applies [World §2.1b](../agentic-trpg/docs/WORLD_SUBSYSTEM_DESIGN.md#21b-容器重量派生与跨操作复用). Distinguish official rules, project adaptations, engineering choices, target design and current implementation. Report conflicts and inaccessible/unverified sources; never silently change rules/architecture or label memory/inference as verified. External sources are development references and do not introduce ordinary runtime online rule lookup.
- On `refactor/scoped-runtime-validation`, preserve the existing unmerged refactor. At source `4341f69cbb560edd9c9c1b1bd907ed6c599c0ed0`, `ContainmentState.weights` is operation-local and `relocate` clears it; cross-operation memory weight caching is a target, not implemented capability. Keep this distinction and the original dated Progress evidence when updating docs.
