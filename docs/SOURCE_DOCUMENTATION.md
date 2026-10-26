# Source documentation maintenance

The project-wide authority is the logical sibling's
[source documentation standard](../../agentic-trpg/docs/CODE_DOCUMENTATION.md).
Resolve siblings from the parallel workspace when using worktrees; report an inaccessible
authority rather than substituting an old branch. New modules, classes and functions need
proportionate docstrings, and changed behavior/constraints require synchronized prose.
Review implementation agreement even when no Skill triggers.

Local workflows live in `.agents/skills/`: `trpg-runtime-change`,
`trpg-locality-review`, `trpg-atomicity-review` and `trpg-code-documentation`.
Their architecture authority remains MC §2.3/§2.3a/§6, with domain responsibilities in
[Session Management](SESSION_MANAGEMENT.md) and the relevant World service document.
They do not add another commit or runtime protocol.

For explanation-only batches, run syntax, applicable Ruff checks and
`python .agents/skills/trpg-code-documentation/scripts/verify_docstrings.py --base <SHA>`.
The verifier checks tracked production `src/**/*.py`, including nested functions;
it preserves executable string expressions and compares signatures/expressions/control
flow after removing only real docstrings. Semantic review and `__doc__` consumer review
remain necessary. Do not run unrelated full game suites just for prose.

See [2026-10-27 scope and evidence](reviews/CODE_DOCUMENTATION_AND_SKILLS.md).

Read [the system overview](SYSTEM_OVERVIEW.md) for repository/module relationships and
state/interface changes. Function details belong in function docstrings, module context
in module docstrings, and repository structure in that overview. A local prose correction
does not by itself require an overview update.
