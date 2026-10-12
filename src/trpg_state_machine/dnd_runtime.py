"""Private compatibility imports; rule execution is composed in CombatHandler."""

from .rules_adapter import delta_changes, validate_dnd_result

__all__ = ["delta_changes", "validate_dnd_result"]
