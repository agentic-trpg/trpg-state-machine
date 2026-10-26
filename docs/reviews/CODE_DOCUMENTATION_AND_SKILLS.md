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
