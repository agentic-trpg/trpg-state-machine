---
name: trpg-code-documentation
description: Maintain and review Python source docstrings when adding or refactoring code or correcting stale explanations.
---

# Source documentation maintenance

Read the single authority at `../agentic-trpg/docs/CODE_DOCUMENTATION.md`, relative
to the repository root, and this repository's AGENTS.md. From this skill directory,
ascend three directories to the root first. Resolve siblings against the logical
parallel workspace; report inaccessible required documents. The SM and RE copies
of this workflow and its verifier must remain identical; neither copy is a second
architecture specification.

1. Inventory maintained production Python modules, classes, functions/methods and
   independently meaningful nested functions. Exclude vendored/generated/temp files;
   do not expand to all tests. Handle data models, Protocols and overloads according
   to their actual structure rather than generating repeated boilerplate.
2. Read the implementation and necessary callers, tests and domain context. Assign
   module responsibility/entrypoints, class data/lifecycle/cooperation, and function
   actions/returns/constraints to their own docstrings. Preserve stronger existing
   explanations. Keep simple functions short and follow the repository language.
3. Explain meaningful effects, exceptions, units/unknown values, cache reuse and
   invalidation, candidate/transaction/savepoint/rollback and old-input isolation.
   Distinguish current behavior from plans; RE results are uncommitted. Do not invent
   state in stateless helpers, rules, callers or performance guarantees.
4. When reusing summaries, verify source hashes, path, owning class and full qualified
   name, including nested scope; never match by short name or old line alone. Read
   changed source again. Leave historical experiment inputs untouched. Current source
   docstrings are the maintained explanation; do not add a second manual summary store.
5. Run syntax and applicable lint/format checks. Use
   `python .agents/skills/trpg-code-documentation/scripts/verify_docstrings.py --base <SHA>`
   for a docstring-only batch. It compares real docstring-stripped ASTs and counts
   coverage; it does not validate prose. Inspect __doc__/getdoc/API/CLI consumers and
   semantically review complex changes. Select tests by actual risk, not all game tests.

Deliver coverage/exclusions, reused/rewritten summaries, exact base and validation
commands/outcomes, omissions and uncertainty. Check relevant DOC/Progress; record
implementation defects without silently fixing them or expanding authorization.
