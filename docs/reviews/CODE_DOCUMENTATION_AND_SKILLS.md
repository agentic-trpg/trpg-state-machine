# Source documentation and Skills — 2026-10-27

Baseline: fetched `origin/main@b9ae704abe4e5d049fa4664873662d7cf991ac39`.
Branch: `docs/source-docstrings-and-skills`, in the isolated parallel task worktree.
The original main checkout's untracked worktrees and user text file remain untouched.
No business logic, public signature, schema, rules, dependency or runtime architecture
changes are included. No merge, PR, Pages or paid retrieval call is part of this batch.

## Coverage and provenance

All 83 maintained production modules, 330 classes and 1,059 functions/methods have
docstrings, including nested functions. Added 270 class and 880 function explanations
across 80 files. Existing 83 module, 60 class and 179 function explanations are preserved.
Tests, examples, vendored/generated and temporary files are outside explanation coverage.

The fixed experiment is documentation repository
`experiment/code-retrieval-ablation@35e79ce8d2ac81d0b62902cff429b52fdda05562`;
fetch found no later remote commit. Its 83 source file hashes match this baseline.
Mapped by path, owning class and full qualified/nested identity, 880 summaries supplied
drafts for missing functions; these were rewritten in English and checked against
current structure, returns, guards and effects. The 179 existing function explanations
were retained. Class responsibilities were written separately. Transaction, command,
rules adoption and memory read explanations received six initial boundary expansions;
a further twelve complex Package, mechanism, command, NPC and evidence entries were expanded.
Historical summaries/labels/rankings were not changed or copied into a second maintained
summary store. See [the shared evidence](../../../agentic-trpg/docs/reviews/CODE_DOCUMENTATION_AND_SKILLS.md).

## Maintenance and installed workflows

[The local development entry](../SOURCE_DOCUMENTATION.md), README, Progress and AGENTS.md
route to the single sibling `CODE_DOCUMENTATION.md` authority. The mandatory rules apply
even without Skill triggering. Runtime principles continue to reference MC, preserving
instance ownership, actual consumer migration, candidate/savepoint/recovery handling,
post-COMMIT publication, legitimate cold rebuilding and bounded retention.

Installed under `.agents/skills/`: `trpg-runtime-change`, `trpg-locality-review`,
`trpg-atomicity-review`, `trpg-code-documentation`. The source documentation workflow
and its verifier are identical to the RE installation. No global configuration changed.
All four metadata validations pass; representative task routing and references were
checked statically, not through a new Codex session's actual automatic triggering.

## Executed validation

| Check | Result |
| --- | --- |
| `uv run --locked --no-sync ruff check src tests examples` | exit 0 |
| `uv run --locked --no-sync ruff format --check src tests examples` | exit 0 |
| `python .agents/skills/trpg-code-documentation/scripts/verify_docstrings.py --base b9ae704abe4e5d049fa4664873662d7cf991ac39` | exit 0; 83 files compile; no non-docstring AST differences |
| Existing docstring preservation comparison | all 322 existing module/class/function values retained |
| Verifier positive/negative checks | six invariants passed, including executable strings and signatures |
| Skill creator quick validation | four installations valid; UTF-8 mode used on Windows |

The existing locked environment was reused through explicit UV_PROJECT_ENVIRONMENT;
no dependency sync or version change was needed. Syntax/AST checks include legal future
imports. Inspected doc consumers and semantics of command/transaction/publication
boundaries; AST equality does not establish prose correctness or new runtime acceptance.
Markdown and diff checks are included at final delivery. No full game suite, new locality/
fault measurements, automatic Skill trigger verification or GitHub Actions pass is claimed.
Existing recovery/multiplayer limits remain under their original acceptance reports.

Final static checks also include the installed verifier's Ruff lint/format. All 23
changed/new Markdown files across the coordinated batch pass local path/anchor, table
and fence checks. Working/staged `git diff --check` is the commit gate. These are local
results; actual automatic Skill triggering and remote CI remain unverified.

## 2026-10-27 follow-up: precise scopes and system overview

Starting HEAD: `a7b0804d5635e185a79ff3c1d1fa6241c2b858fe`, existing branch
`docs/source-docstrings-and-skills`, same worktree. After fetch, origin's task branch
matches, with no later commit; origin/main remains `b9ae704`. The task worktree started
clean. No branch creation/merge, unrelated work, experiment or Pages changes.

### Two corrected docstrings

- `world_mechanisms.py::MechanismService.automatic` handles **one source event**.
  `events, fired = derived, set()` creates per-call deduplication; matching candidates
  can cause at most one operation per mechanism for that source event. Another event
  can trigger the same mechanism if lifecycle/frequency/cooldown and other checks allow.
  This path calls the maintained `_scene_index(sid, scene)` directly. Although
  PackageEffects passes `prepared=mechanism_index`, automatic does not consume it;
  `_batch_index` is used by the separate component-destruction path. Index sharing
  and activation deduplication are distinct. The corrected explanation preserves
  command/source-event identities and the caller's bounded expansion/shared transaction.
- `grid_maps.py::_WallIndex.query` selects candidate walls by **bounding-box overlap**,
  including boundary touches; a candidate segment need not intersect the query region.
  ReadOnlyTopology._ray_topology supplies candidates to `_step_is_legal` and
  `has_line_of_sight`. Both downstream GridTopology methods retain exact
  `_segments_intersect` checks, verified in sibling source and SM's fixed RE
  `a011eba3ea2226e22d8081294ee38d3ec193b325`. No geometry algorithm/check was changed.

The previous descriptions overstated deduplication and geometric precision. Only these
two docstring values changed; no trigger logic, deduplication range, signature,
expression or control flow changed.

### Overview and maintenance

Added [docs/SYSTEM_OVERVIEW.md](../SYSTEM_OVERVIEW.md), 150 lines: public entrypoints,
creation/load/unload, commands and queries, domain collaboration, version/idempotency,
fixed rule inputs and adoption, event/reliable work and NPC paths. State tables distinguish
SQLite facts, consistent active RAM, owner fields/indices, fixed views and candidates.
COMMIT precedes controlled RAM publication; unknown commit/publication failure and
supported reconciliation are separate from rollback. Full recovery/multiplayer and other
unaccepted goals remain explicitly limited.

Changed files: `AGENTS.md`, `README.md`, `docs/SOURCE_DOCUMENTATION.md`,
`docs/SYSTEM_OVERVIEW.md`, the two production files above, this report,
`.agents/skills/trpg-runtime-change/SKILL.md`, and
`.agents/skills/trpg-code-documentation/SKILL.md`.
The entrypoints route structural changes through the overview and relevant authority;
local function prose does not require rereading every architecture document or editing
an unrelated overview. Runtime Skill changes only add an ownership/flow synchronization
checkpoint; the documentation workflow remains identical to RE's copy.

### Executed checks and limits

| Check | Result |
| --- | --- |
| `uv run --locked --no-sync ruff check src tests examples` | exit 0 |
| `uv run --locked --no-sync ruff format --check src tests examples` | exit 0 |
| `verify_docstrings.py --base a7b0804d5635e185a79ff3c1d1fa6241c2b858fe` | exit 0; all 83 production files compile; stripped AST equal; exactly two files changed |
| Overview source navigation | 107 file/symbol references across all three overviews verified |
| Markdown | 20 changed files across the batch pass path/anchor, table and fence checks |
| Skill metadata and shared copy | six installations validate; SM/RE common workflow identical |
| Working/staged whitespace | `git diff --check` and `git diff --cached --check`, exit 0 |

No Mermaid is used; diagrams need no rendering validation. No game suite or new runtime/
fault/performance acceptance was run for this explanation-only continuation. Prior test
counts above remain dated historical evidence, not new results. Automatic Skill triggering
and GitHub Actions are unverified. No business defect was opportunistically fixed.
