"""Test helpers for the confirmation flow (D31): propose → customer yes → same call runs."""
from __future__ import annotations

from typing import Any


def confirmed_run(gate: Any, session: Any, tool: str, args: dict, answer: str = "yes") -> dict:
    """Call a write the way the agent does: the first call returns the summary, the customer answers, then the
    same call runs."""
    first = gate.run(session, tool, args)
    if not first.get("needs_confirmation"):
        return first
    session.on_customer_message(answer)
    return gate.run(session, tool, args)
