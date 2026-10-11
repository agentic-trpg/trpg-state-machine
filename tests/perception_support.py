"""Explicit hand-authored Judgment for synthetic post-perception domain tests.

These tests exercise Inbox/EVA/atomicity, not automatic sensory detection. The
runtime still performs admission first, and never calls this for known blockers.
No Rule Engine result, evidence, deterministic branch or database row is mocked.
"""

from trpg_state_machine.perception import JudgmentResult, SignalJudgment


def synthetic_notice(request):
    return JudgmentResult(tuple(SignalJudgment(s.signal_id, True) for s in request.signals))
