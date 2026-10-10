# SM-12: authoritative noncombat checks

Use the upstream `CheckPayload` with all adjudication fields explicit and
`TypedCommand(command_type="rules.check", ...)`. The authenticated adapter must
set `TrustedContext.can_adjudicate_checks=True` only for an authorized GM.
This is an in-process capability, never deserialized from a player's payload.
NPC subjects additionally require their existing control grant. Authorization
is checked before receipt replay and again before committing. The library does
not implement a GM authentication service or infer DC/advantage from prose.

The actual Engine computes ability/skill checks, proficiencies, expertise and
generic explicit flag advantage/disadvantage. The admitted result is exactly an
empty StateDelta, one matching CheckRolled and a complete RNGTransition. A rolled
failure is still an accepted evaluation and a committed command. The event
records adjudication inputs and trusted principal for audit. It creates no
perception signal containing DC, modifiers, roll or outcome; NPC inboxes continue
to contain only previously authorized perceptions.

For this bounded path, a committed check changes the authoritative RNG and
advances world_version once; event_seq advances for committed events separately.
This is a local mechanical check policy, not a decision for all event-only or
Meta commands. RNG, event, receipt and outbox use the existing atomic transaction.
No duplicate roll on replay. A conflict discards the evaluated candidate.

Missing DC is rejected as `pending_choice_unsupported` before calling evaluate,
with an ordinary durable preflight refusal receipt and zero RNG/event changes.
A new adjudicated request needs a new command ID. Unexpected needs_choice after
admission is an independent invalid_rule_result with no receipt. Engine rejected
and unsupported remain distinct codes (e.g. actor_invalid vs check.capability)
within the existing command receipt's rejected status. Schema/binding/runtime
failures remain independent, retryable failures, never permanent business refusals.

Tools, resources/granted dice, contextual/sense checks and derived advantage
sources remain unsupported. Saving throws, formal PendingOperation, spells and
general adjudication services are outside scope. Complete mechanical inputs are
required for every currently present Actor; missing scene/actor facts fail closed.

36 new tests exercise actual success/failure, modes, proficiency/expertise,
empty-delta parity, private projection, missing DC, NPC/GM authorization/revocation,
result faults, SQL rollback, concurrent world changes and a fresh Python process.

The capability supplements the existing Session principal/player check; it does
not authorize a different principal to access an owner's Session. Integration
authentication and issuance/revocation of these contexts remain Host duties.

Final verification: 380 tests passed, two upstream deprecation warnings.
`uv sync --locked --group dev`; `uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/b12-full-final`;
Ruff check, Ruff format check and `git diff --check` all passed.
