---
name: trpg-runtime-change
description: Change SM instance fields, caches, indices, Snapshots or state writes while preserving owner and publication boundaries.
---

# Runtime change

Read repository AGENTS.md, the logical sibling's `docs/MODULE_CONTRACTS.md`
§2.3/§2.3a/§2.4/§6.5–§6.6 and relevant domain docs. Resolve the sibling relative
to the repository root, not this skill folder. Use `docs/CODE_DOCUMENTATION.md`
in that sibling for explanation maintenance; report missing authority documents.

Trace the public operation to the authoritative fact owner and existing runtime
field owner. Inventory all writers (including background, time, effects, Meta/Task,
configuration, import and maintenance), affected entities/relations and consumers.
State which versions and dependencies change and which valid fields consumers reuse.
Distinguish supplied fixed input, isolated mutation candidates and published current
state; a newly added cache with no production consumer is not completion.

Integrate maintenance with existing persistence/UoW, commit/publication, savepoints,
rollback, recovery invalidation and lifecycle retention. Use existing owner replacements,
not a second transaction or Session authority. Cache hits retain authorization/OCC,
bindings/RNG/idempotency/disclosure requirements. Preserve cold loading, lazy rebuild
and bounded memory under the authority contract.

Choose public-path behavior, failure and fixed-target/growing-unrelated-data counters
for the actual risk. Inspect SQL, RAM, old references, versions, indices and caches.
Update affected docstrings and evidence-backed DOC/Progress. Deliver owner/writer/
consumer coverage, validity and failure boundaries, reproducible checks and explicit gaps.
