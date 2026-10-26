---
name: trpg-locality-review
description: Review SM scans, repeated reads, assembly, calculation and cache hit costs through real public call paths.
---

# Locality review

Read repository AGENTS.md and the logical sibling `../agentic-trpg/docs/MODULE_CONTRACTS.md`
§2.3/§2.3a/§6.5–§6.6 from the repository root; use relevant domain docs for scope.
Do not infer cost from a function name, a target ID argument or an affected set.

Trace real public consumers through direct and indirect calls. Separate cold loading,
invalidated rebuilding, mutation maintenance and valid hits. Classify SQL statements/
fetched rows, enumeration, JSON parsing, model/Snapshot construction, projections,
dependency validation, copying (including complete index copies) and serialization.
Distinguish dictionary access, bounded local traversal and global scans; include any
upper wrapper that repeats work even when a lower helper caches successfully.

Hold target scope fixed and grow unrelated scenes/objects/actors/items. Instrument
deterministic counters across the public path and compare controls, reporting necessary
aggregate/output work separately. Timing and static findings alone do not establish
scaling. Identify actual cache validity writers and lifecycle/rollback/recovery behavior.
Keep legitimate full outputs, initialization/recovery diagnostics and complete mechanical
closures distinct from ordinary local operations. Do not remove safeguards on inference.

Deliver exact source/baseline, consumer chain, measurements and reproduction commands,
allowed scope, confirmed defects versus hypotheses and unmeasured boundaries. Review
alone does not authorize business refactoring; use the task's actual authorization.
