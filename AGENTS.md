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