# GC-1 spatial world interactions review

Repository: `D:\Projects\agentic-trpg\trpg-state-machine`.
Remote: `https://github.com/agentic-trpg/trpg-state-machine.git`.
Branch: `feat/sm-gc1-world-interactions`; baseline `058e79f300016f7397f0c3401bc03b58554e2b71`.
Only SM code/tests/examples/docs are in scope. The revised user task supersedes
abstract placement; historical coordinate recovery is explicitly excluded.

## Implementation review

Independent read-only reviewer examined position authority, exploration/combat
projection, terminal freeze, legacy movement/helper/NPC output admission, temporary
movement versus visual grids, authorization errors and transaction rollback.

Confirmed findings and fixes:

- Hidden object and passage queries distinguished existing references; source
  visibility now precedes capability/precondition disclosure. Fine legacy exits
  also use generic `invalid_target` for unauthorized/nonexistent routes.
- Placement originally ignored all closed doors; its exemption now covers only
  the handled source/target threshold from that endpoint's own standing point.
- Traversal could enter an unsupported private fine Scene; target public scope
  is checked before committing departure.
- Mechanical `additional_scenes` could disagree with the spatial Grid; loader
  now checks both initial and additional Scene geometry.
- Preinitializing remote entities counted a PC visit; remote fine Scenes start at
  zero, supported by Schema 21's transactional visit-count table rebuild.
- Abstract /2 null-state source objects needed Evidence /5; capture selects the
  extended schema for spatial or null-state object Packages.
- A closed door located on its own threshold blocked opening reach. Switch reach
  now exempts only that door's current-side `(passage_id, threshold_cell)`; another
  door even on the same Cell remains a blocker. A public-API regression covers it.
- A private target's scope was checked before the hidden source endpoint. Source
  authorization now precedes target scope; the hidden-reference regression covers
  public/private targets and legacy PC/NPC movement separately.

The final independent complete-diff read-only review found no remaining blocking
findings, current-position double authority, authored-position reset on combat
start or post-close rewrite of terminal actor_zone. It independently reproduced
opening/traversing a threshold door and indistinguishable hidden-private/unknown
exit refusal. Code, new files, docs and example limits were all included.

## Validation ledger

Commands use `PYTHONUTF8=1`, `UV_OFFLINE=1` and a repository-local UV cache; locked
dependencies are already installed. Process/concurrency tests run in the authorized
host environment because sandbox child-process initialization stalled.

- Initial Fast: 76 passed.
- Package/session regression: 96 passed.
- Spatial/session/real combat focused regression after initial review: 94 passed.
- Expanded perception/queue/migration run: 157 passed, 2 failures, both subsequently
  fixed (test assumed a stale NPC state version; a session snapshot unnecessarily
  queried sensory tables for an abstract zero-signal operation).
- Focused confirmation of those fixes: 35 passed.
- Initial full regression: 853 passed, 12 failures. Failures were resolved rather
  than skipped: added-column history assertions now compare every original column
  exactly, abstract API compatibility is retained, and dead-PC world movement is
  explicitly refused after TPK. No threshold was reduced.
- Full regression before the last two review fixes: 874 passed, 2 existing Lark
  deprecation warnings, 397.26s; superseded by the final run below.
- Final Fast (`tests/test_package.py tests/test_commands.py tests/test_sessions.py`):
  110 passed, 7.80s. Final GC-1 focused (`tests/test_spatial_world.py`): 36 passed,
  8.80s. Both retain the same two dependency deprecation warnings.
- Both structured object examples (crate/stool) passed, including independent
  process state comparison and exact command replay.
- `ruff check .`, `ruff format --check .` (128 files) and `git diff --check` passed.
  `git diff --exit-code HEAD -- pyproject.toml uv.lock` passed: no RE/SRD upgrade.
- Final full regression (`uv run --offline --locked python -m pytest -q`):
  **876 passed**, 2 existing Lark deprecation warnings, **384.33s**. All final
  business-code changes and regression additions are included; no skips/failures.
- Final structured examples each executed 12 commands with world version 10,
  unchanged RNG, exact receipt replay and equal fresh-process state. Manual
  abstract null-state source capture validated Evidence /5; new local doc links
  also passed validation.

New functional evidence uses real SQLite and public command APIs. It covers two
objects, two maps, both directions/one-way exits, walls/corners/occupancy, gates and
multiple blockers, actor control, query purity, no-op, replay/OCC, fresh interpreter
restore and revisit, frozen evidence, reliable NPC handoff, lost responses and
mid-command domain/SQL rollback. Existing actual-schema migration tests additionally
crash after Schema 21 DDL and verify rollback, concurrent recovery and old-column
equality. Real RE tests inspect the actual evaluator request and legally close the
encounter before checking exploration/terminal-history independence.

See [implementation and support matrix](../GC1_SPATIAL_WORLD_INTERACTIONS.md).
