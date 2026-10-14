# World W1–W8 implementation record

Baseline: `1fa7cea5dab7bb95f3f6fa975f8e681c691937a6`; remote main verified at that
commit on 2026-10-15. Implementation uses an isolated clone on `feat/world-w1-w8`
under `.test-tmp/world-w1-w8`. The original checkout's uncommitted GC5 changes are
excluded and preserved. No AGENTS.md, check-fast, or CI workflow exists at this
baseline. Checks use pytest, Ruff check/format and git diff --check.

Architecture read-only reference: `agentic-trpg@acb446bb97241aa32e0d12c35b02bf30dd7d9eae`,
MODULE_CONTRACTS §0.3–0.6 and §3–6, WORLD_SUBSYSTEM_DESIGN. Candidate ABI examples
are not implemented protocols. Session, WorldFacts, CharacterFacts, Activity,
Effects, CombatRecords and Perception keep their existing authority boundaries.

## API baseline and shared contracts

Writes use `submit_command(TypedCommand, package, context=TrustedContext)` and
`submit_command_async`; `sm-command/1`, existing fingerprint, expected world
version, receipts and savepoint/transaction. Context is authenticated by caller
integration, never parsed from payload. Rejected/unsupported actions produce
rejected receipts with error_code; stale versions produce conflict/version_conflict.
Storage errors roll back the whole transaction. Replays use existing command receipts.
Queries are synchronous detached reads. `get_command_receipt` supports lost replies.
There is no HTTP server or authentication implementation in this library.

Legacy player/NPC command paths: StepActor, TraversePassage, LockDoor/UnlockDoor,
PickupItem/DropItem, navigation activities; NPC Evaluation has explicit existing
intent conversion. New operations will document NPC conversion separately.
All World changes retain active-combat guards. Queries do not authorize execution.
SetObjectState and PackageEffects are audited with each capability extension.

## W1 — explicit definitions and authorized queries

Package `/6` adds immutable `WorldObjectDefinition` records in `world_objects`.
Unknown capabilities, duplicate capabilities, invalid identity references and
contradictory initial open state are rejected during complete Package validation,
before any session initialization. Only implemented openable is currently declared.
Legacy `/1`–`/5` digest and loading behavior remain unchanged; legacy openable/item
capabilities remain enabled through their existing contracts.

Public synchronous queries: `get_object(sid, actor_id, object_id, package, context=...)`
and `list_scene_objects(sid, actor_id, package, context=...)`. Actor control/NPC access,
public scene policy and actual visibility filter objects. Credential lists are absent.
Same capability works across declared object identities; names do not select behavior.

| Suggested fields | Actual location | Class | Writer | Consumer/status |
|---|---|---|---|---|
| session/object/instance ID | sessions + objects.object_id | R | existing initialization | same portable identity, queries |
| definition/template ref | WorldObjectDefinition.definition_ref | T/R | Package validation | WorldFacts.object_definition, object view |
| package/definition revision | sessions.package_digest/version | T/R | Session creation | pin checked on every entry; no redundant per-object revision |
| display_name, description_ref | WorldObjectDefinition | T | Package | authorized query |
| material, size_category, weight_lb | WorldObjectDefinition, nullable unknown | T | Package | query; no material-derived mechanics |
| capabilities | tuple of strict capability dataclasses | T | Package | capability routing; unknown rejected |
| initial placement | ObjectDefinition.initial_cell / PortableItem.initial_owner_id | T | Package | existing one-time initialization |
| scene/cell/carrier | objects; actors for carried origin | S/R | WorldFacts + CharacterFacts | single actual location |
| effective_capabilities/object_view | WorldObjectService.view | D | none | authorized queries; no state copy |
| object_kind | deferred | T conditional | none | no classification consumer in W1 |

No DB migration is necessary for fixed definition binding: Session already pins
the complete Package and object identity resolves within it. Restoring does not
reapply initial values. Creature identity stays with actors/CharacterFacts.

Validation: `pytest tests/test_world_definitions.py tests/test_lockable_passages.py -q`
(with checkout-local basetemp). Includes a separate Python process restore,
unauthorized query, strict validation and legacy lock/navigation compatibility.
Initial environment-only failures were fixed by selecting a writable basetemp.
