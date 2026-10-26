---
name: trpg-atomicity-review
description: Review SM SQL, UnitOfWork, SessionMemory, savepoints, post-commit publication and recovery changes.
---

# Atomicity review

Read repository AGENTS.md, `docs/SESSION_MANAGEMENT.md`, relevant local acceptance,
and logical sibling `../agentic-trpg/docs/MODULE_CONTRACTS.md` §2.3/§6 from the
repository root. SQL COMMIT and RAM publication are not one ACID transaction;
apply the existing pause/reconciliation contract rather than inventing a coordinator.

Trace actual writers, candidate stages, SQL boundaries, publication and success/current
projection paths. Select meaningful failure injection points from the modification's
risks; do not mechanically run every fault scenario for a small change.

Compare persisted DB, current RAM, old references/fixed Snapshots, fact/dependency
versions, indices and caches. Relevant cases include a caught SQL statement exception
followed by continued execution, savepoint rollback and subsequent writes, full rollback,
unknown commit outcome, durable commit followed by RAM publication failure, lost response/
receipt retry and restart or cross-connection recovery. Assert which state is visible,
which access must pause and how original outcomes are recovered. Retain authorization,
OCC/bindings/RNG/idempotency and candidate/old-reader isolation.

Deliver exact source and reproduction commands, injected boundary and observed state
matrix, selected and omitted cases with reasons, remaining risks and evidence-backed
DOC/Progress changes. Do not label unexecuted scenarios accepted or fix unrelated defects.
