"""Structured failures at package and runtime boundaries."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    path: str
    message: str


class StateMachineError(Exception):
    """A rejected operation; callers can inspect code without parsing prose."""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        self.issue = Issue(code, path, message)
        super().__init__(message)

    @property
    def code(self) -> str:
        return self.issue.code
