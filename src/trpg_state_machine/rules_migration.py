"""Explicit complete-predecessor Package conversion, never a live-save reset."""

import copy

from dnd5e_engine.evaluation_migration import migrate_snapshot_16
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBinding, ruleset_binding

from .errors import StateMachineError
from .package import _validate, canonical_json


def migrate_package_16(data, *, loader, package_version):
    """Return new approved source content; does not access or repin any Session.

    Only full /16 facts with the original /35 same-data binding can be proven.
    Actual old SM /1-/2 saves lack these facts and remain audit-only.
    """
    value = copy.deepcopy(data)
    try:
        source = RulesetBinding.model_validate(value["manifest"]["ruleset_binding"])
        if package_version == value["manifest"]["package_version"]:
            raise ValueError("migration requires a new Package version")
        rules = value["rule_bindings"]
        if not rules or set(rules) - {"combat_foundation", "noncombat_foundation"}:
            raise ValueError("unknown mechanical foundation")
        for foundation in rules.values():
            converted = migrate_snapshot_16(
                canonical_json(foundation["snapshot"]), source_binding=source, loader=loader
            )
            RNGState.model_validate_json(canonical_json(foundation["rng_state"]))
            foundation["snapshot"] = converted.model_dump(mode="json")
        value["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
        value["manifest"]["package_version"] = package_version
        _validate(value)
    except (StateMachineError, ValueError, TypeError, KeyError) as exc:
        raise StateMachineError(
            "unsupported_rule_migration", "Source facts/binding cannot be proven"
        ) from exc
    return value
