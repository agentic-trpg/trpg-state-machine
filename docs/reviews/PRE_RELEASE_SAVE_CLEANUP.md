# Pre-release save compatibility cleanup

2026-11-14; branch `codex/pre-release-save-cleanup`; baseline `009fb32ccc38f284748b01f4e3a057b134c74342`.

## Decision and implemented scope

The project has not released a save format. Current acceptance covers fresh databases and
Schema41 saves. `SqliteStore` inspects existing files read-only before opening a writable
connection, rejects versions 1–40, and repeats the version check under its existing migration
write lock. An unversioned database must have no schema objects. Rejection does not run
Schema41 `DROP COLUMN` statements or discard old inventory. Fresh databases still apply
the bundled SQL sequence under the original migration transaction; Schema41 reopen keeps
its integrity checks, trigger installation check, and existing recovery behavior.

The explicit `upgrade_instance_configs` API, old inventory migration/preflight CLI, and
perception queue/history backfills were removed. Their historical-only positive tests and
unused archived-runtime pytest fixtures were retired. The current corruption test for a
missing carried-object config and the required spatial-trigger rejection were retained.

`objects` remains the inventory fact owner. Synthetic-evaluation core inventory and
combat-only PC item seeding are current Package creation paths, so their stable item IDs and
validation remain. No RE interface, ruleset pin, command receipt, UnitOfWork, SessionMemory
publication, or OCC contract changed.

## Deliberate residuals

- The earlier SQL files remain necessary to construct an empty Schema41 database from version 0.
  This batch does not consolidate them into a new base schema.
- `storage_migrations.reconcile_schema34` still checks the current mechanism layout and indexes.
  Its version-34 reconciliation branch cannot run through the new version gate; simplifying the
  historical branch is outside this narrow cleanup.
- `CharacterFacts._retire_legacy_item_identity` and its WorldFacts writer remain in the
  fresh foundation initialization call chain. No successful current replacement route was
  proven during this batch, and removal needs a separate reachability review across all
  admitted foundations; this passing candidate leaves the bridge unchanged.
- Dated migration reports and standalone archived worker scripts remain historical evidence.
  They do not run in ordinary opening or the collected current test suite.
- Active-combat save/resume remains unsupported by the existing MVP boundary.

## Local verification

- `pytest --collect-only -q`: 2063 tests collected, exit 0. No positive pre41 upgrade case
  remains in the collected migration selection; the prior archived-worker files remain unused.
- Final focused policy/fresh migration lock/crash/current config rejection selection:
  8 passed, 2 dependency warnings, exit 0.
- Current-path combined selection (policy, fresh migration lock/crash, item transfers,
  container, containment, Session Management): 101 passed, 2 dependency warnings, exit 0;
  executed outside the restricted Windows loopback sandbox after the sandboxed run stalled.
- `ruff check src tests examples`: exit 0. `ruff format --check src tests examples`: exit 0
  (221 files already formatted). Current test selection does not prove the full game suite.
- Pre41 orphan WAL: `mode=ro` reads the committed version before writable open; the main DB and
  WAL SHA-256 digests remain unchanged on refusal. The SHM file is transient SQLite metadata
  and can change during a read-only WAL lookup.

No commit, push or merge is part of this branch's local work.

## Independent review, round 1

Passed on the frozen local candidate based on
`009fb32ccc38f284748b01f4e3a057b134c74342`. The reviewer verified 44 source
and test paths before and after the run using SHA-256
`22A19895D8836FEC09368309203F06357ED07DF06C64CAFF0ADBB4D6335EC596`.
The independent selection passed 148 tests in 79.69 seconds. It covered current
database opening and rollback, old-save refusal with pending WAL frames,
container and item actions, missing carried-object configuration and missing
spatial maintenance triggers. Ruff check, format check, and diff check passed.
Collection completed with 2,063 cases. Full-suite execution is outside this
evidence. The old inventory identity replacement helper remains in source.
Merge awaits user authorization.
