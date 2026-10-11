# Batch B — Event-local / Signal-scoped Perception Evidence

Date: 2026-10-12. Baseline: `5deea78` (SM-PER-1).
Branch: `fix/sm-per2-event-local-evidence`. Only State Machine is modified.
Architecture: readonly Meta `6e30271`, MODULE_CONTRACTS v0.10 §7 / §12.5 and
STATE_MACHINE_ARCHITECTURE v0.12 §3.6 / §6.2 (Batch A).
Engine / SRD remain pinned to `602dcb8d448e670049427bbb751d6ed226005298`.
No accepted architecture conflict was identified. Concrete evidence fields and
producer phase codes below are local versioned implementation, not frozen ABI.

## Root cause and actual change

Previously `_append_events()` captured one `event-evidence/3` from the final
database state and reused it for every event in a command. All instantiated
Actors and Scenes were copied before actual Signals were constructed. Neither
event sequence nor a shared world_version / tick proved identical event stages.

`events.py` now constructs Signals and freezes evidence at the actual producer
call site in a private `_StagedEvent`. This is a transaction-local candidate;
its frozen JSON is not an independent authoritative state or durable queue.
`service.py:_append_events()` assigns the unchanged event ID / sequence / source
command linkage and writes staged Signals / Evidence / Outbox. It never samples
world state. The committed world_version is bound there; it describes the atomic
commit, not an assertion that all event stages are identical. Game tick is sampled
at capture. Existing breadth-first cascade order and trigger history are retained.

## Proven producer stages and limits

| Producer / files | Capture stage | Evidence and verification |
|---|---|---|
| Session creation, `service.py` | After Scene, PC and admitted noncombat state initialization, before Package cascade | Entry source is the real selected PC. Existing session rollback / isolation tests remain. |
| EnterScene, `service.py` | Exit BEFORE departure and target initialization; entry AFTER PC arrival | Real command emits seq 3/4 with PC in different Scenes and different NPC candidate sets, while sharing one committed version. Restart, out-of-order projection and replay verified. |
| SetObjectState / SetSceneFlag / MoveActor, `service.py` | Immediately after the operation's own state change | Real cascade opens then closes the same object in one command. Saved source states are open / closed / closed while final DB is closed. Real NPC move captures destination without copying identity. Flag events have no Signals. |
| Package event, `events.py:_expand_events` | Immediately after its declared effect, before later consequences | Actual Package trigger/effect order, source object state and per-Signal candidate union verified; no fabricated Evidence to prove timing. |
| Dialogue, `npc_outputs.py` | At committed publication | Actual authorized Evaluation Result → dispatch → persisted auditory Signal, speaker evidence and idempotent output tested. |
| Game Time / Schedule, `game_time.py` | After clock change / each Schedule lifecycle record | Currently zero Signals, no sensory state claimed; actual tick 10 plus two NPC lifecycle events, restart and replay tested. Schedule Runtime update semantics are unchanged. |
| Combat initialization, `combat.py` | After initialization | Currently zero Signals, no stage-dependent perception claims; existing real Engine tests cover commit/RNG/rollback. |
| D&D Result, `dnd_runtime.py` | Intermediate phase NOT exposed | Nonempty sensory events record unresolved phase `engine.intermediate_phase_unavailable`; no final-state sensory copy, callback or OB. Real accepted attack and two cross-phase attacks verify this. Zero-Signal mechanical records need no sensory snapshot. |
| Synthetic ProposedEvents, `mechanical_runtime.py` | Intermediate phase NOT exposed | Same conservative policy with `synthetic.intermediate_phase_unavailable`. Actual typed world effects still stage through `_operate`; existing synthetic pipeline tests pass. Nonempty synthetic ProposedEvent phase recovery is not independently demonstrated. |

Readonly installed Engine evidence: `evaluation.py:evaluate()` executes the live
intent on detached state, then returns `attack_delta(snapshot, after)` and the
ordered event-log suffix. It supplies no per-event snapshots / deltas or capture
callback. `orchestrator.py` emits IntentSubmitted before action payment; that
marker is not proof of an external action-start or noticing phase. This batch
does not relabel it Action Started, replay rules to reconstruct history, or
change Engine contracts. A correct upstream stage/Signal protocol remains OPEN.

## Evidence / discovery / ownership

New local JSON version: `event-evidence/4`, in existing `event_evidence` rows.

- `phase`: captured / unresolved plus producer reason code.
- `discovery`: explicit complete marker, `by_signal` candidate IDs and gap reason.
- `actors`: only the union of Scene candidates, including Blinded / Deafened /
  inactive NPCs. Discovery does not test senses, visibility, conditions or notice.
- `source_actors`: only origin Actors not already in candidates (including PC).
  Source NPCs that are candidates reuse the same saved state.
- `objects`: only origin objects, with their event-stage Scene and state.
- `scenes`: only Signal Scene environments, required Grid / unsupported-effect
  flags. Original Signals retain Origin cells and Stimulus. RulesetBinding,
  candidate/source Conditions, senses, traits, alive/dead context and available
  combat cells come from the same trusted authority as SM-PER-1.

For each supported Scene, capture enumerates every instantiated NPC without
limits or judgment. Projection validates the complete marker, exact Signal keys,
candidate membership and union against saved positions, unique identities and
environment/source structure. It never repeats Scope search against current DB.
Missing / inconsistent evidence stays Unresolved; it is not an empty result or
input for a callback to invent mechanical facts. Unknown Package Scope and unknown
Scene references fail validation. An uninstantiated Signal Scope records
`missing_scope_scene`, which stays unresolved after the Scene is later entered.

Measured structural clipping in real tests: with two instantiated NPCs / two
Scenes, an exit or entry stores ONE candidate and ONE Scene, plus the source PC;
the unrelated NPC / Scene and all unused objects are absent. Multi-Scene Signals
store TWO candidates / TWO Scenes and only TWO required objects; duplicate
same-Scene Signals and observer/source identity share records. Zero Signals store
zero observers, sources, objects and environments; a SQLite authorizer proves no
mechanical Actor/Scene read. Zero candidate NPCs are explicitly complete, with
the required PC source retained. No byte-size or throughput benchmark is claimed.

No full World snapshot, second Inbox, Timeline, independent service, ACK, cursor
redesign, new Signal channel, radius, formula, JEV model or NPC cognition is added.
Visual/Auditory conditions and real Engine mechanical admission are unchanged;
mechanical visibility remains distinct from perceived=true.

## SQLite / compatibility / atomicity

Database stays Schema 18. No DDL or historical rewrite is needed; only new JSON
records use `/4`. The Package canonical digest algorithm and old four-field
Signal acceptance stay unchanged. A minimal Package validation extension allows
separate Signals to scope different declared Scenes; each Origin is validated
against its own Signal Scene, not the trigger Scene. No new Scope kind is added.

Completed `/1`–`/3` work / OB / Meta / Receipt stays immutable. Uncompleted
nonempty `/1`–`/3` evidence cannot prove the new stage/completeness requirements
and stays Unresolved; known-version empty Signal events may complete without OB.
Unknown/missing evidence cannot silently complete. Existing pre-perception legacy
boundaries are unchanged. A real archived `5deea78` runtime creates two SQLite
Sessions, one projected and one pending; reopening twice preserves their data,
OB IDs and evidence, never invokes judgment for unproven history, and writes new
events as `/4`. Old Schema migration coverage remains in the complete suite.

State/Delta/RNG, events, Signals, frozen Evidence, Outbox, trigger history and
Receipt still commit inside the original transaction/savepoint. Injected SQLite
failures at Evidence, record, Outbox and Receipt insertion leave all real tables
unchanged; exact command retry commits once. A real rules.check plus conditional
world effects also proves Delta/RNG rollback and safe retry. Capture performs no
network or evaluator invocation inside the write transaction.

Global contiguous cursor behavior is deliberately retained: unresolved Engine
stages, legacy history and missing Scope evidence can still hold later correctly
projected OB behind a gap. No silent skipping, extra ACK or recovery migration is
introduced. Completed work is not reinterpreted on upgrade.

## Regression evidence and review

New regression file: `tests/test_event_local_evidence.py`. Existing tests in
`test_perception.py`, `test_perception_conditions.py`, `test_dnd_hardening.py` and
`test_cross_phase.py` retain mechanics / persistence / privacy assertions.
Out-of-scope NPCs now correctly have no result row, and tests additionally verify
complete discovery exclusion. Two prior vertical tests expected OB from final
Engine state: they now assert real attack Signals still commit, unresolved phase
is durable, no perception result/OB is fabricated and the cursor stays held.
No test is deleted, no judgment gate is relaxed.

`PER-19-22-01` remains a real SQLite regression in
`test_per_19_22_01_dark_scene_no_false_positive_sqlite_restart`; it passes with
`/4`, persisted darkness, actual legal command, restart, projection, no false
visual OB/Inbox/Work, unchanged RNG/state and receipt replay. The SM-PER-1 table
tests still exercise real Engine light, senses and Condition behavior.

Executed validation (all passed):

| Command | Actual result |
|---|---|
| `uv sync --locked --group dev` | Locked 19 packages resolved / checked; dependency files unchanged. |
| `uv run --locked python -m pytest -q tests/test_commands.py tests/test_events.py tests/test_perception.py tests/test_game_time.py` | Fast: **76 passed**, 18.52s. |
| `uv run --locked python -m pytest -q tests/test_event_local_evidence.py tests/test_perception.py tests/test_perception_conditions.py tests/test_dnd_hardening.py tests/test_cross_phase.py` | Focused: **134 passed**, 31.59s. |
| `uv run --locked python -m pytest -q` | Full: **792 passed**, 236.97s; baseline 768 + **24 new**, no skips or failures. |
| `uv run --locked python -m ruff check .` | All checks passed. |
| `uv run --locked python -m ruff format --check .` | All files already formatted. |
| `git diff --check` | Passed. |

Pytest reports only the two existing Lark `sre_parse` / `sre_constants`
DeprecationWarnings. Tests exercise real SQLite tables and real installed Engine;
notice callbacks in synthetic protocol cases are explicit test adjudication, not
evidence that Engine can automatically notice an event.

Targeted final review checks producer order, every append/expand caller, strict
evidence decoding, source ownership, candidate completeness, write savepoints,
event IDs, failure replay, historical completion and unchanged dependency lock.
No new P0/P1/P2 has been confirmed in this implemented scope. This is not a
claim of complete Engine temporal perception: the upstream intermediate-stage
contract, historical controlled recovery, formal checks, JEV and unsupported
conditions / propagation remain OPEN. Batch B's proven world-producer fix can be
reviewed independently; end-to-end Engine observation delivery is still blocked
by unavailable stage evidence. No global pre-merge approval is claimed.
