"""LLM user simulator: plays the customer from a task's scenario, τ-bench style, so evals are whole conversations."""
from __future__ import annotations

import os
import time
from typing import Any

import anthropic

from .agent import Usage

STOP, TRANSFER, OUT_OF_SCOPE = "###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###"
END_TOKENS = (STOP, TRANSFER, OUT_OF_SCOPE)

GUIDELINES = f"""You are playing a customer contacting an online retail store's customer service chat.
Simulate a realistic customer while following your scenario instructions exactly.

Rules:
- Write one short chat message at a time, like a real customer typing.
- You only know what is written in your scenario. If asked for anything else (email, address, order id,
  preferences), say you don't know or don't remember. Never invent information.
- Share details only when asked, one or two at a time. Start with a short, natural description of your issue.
- When the agent lists an action and asks you to confirm, answer according to your scenario. If it matches what
  you want, say an explicit "yes".
- Agreeing to an action is not the action being done. Wait for the agent to confirm it is completed.
- Before ending, make sure every request in your scenario has been handled.
- When everything in your scenario is done, reply with exactly {STOP}
- If you are transferred to a human agent, reply with exactly {TRANSFER}
- If the scenario gives you no way to continue, reply with exactly {OUT_OF_SCOPE}
"""


def scenario_text(task: dict[str, Any]) -> str:
    us = task["user_scenario"]
    ins = us["instructions"]
    parts = []
    if us.get("persona"):
        parts.append(f"Persona: {us['persona']}")
    for key, label in (("reason_for_call", "Reason for contacting"), ("known_info", "What you know"),
                       ("unknown_info", "What you don't know"), ("task_instructions", "How to behave")):
        if ins.get(key):
            parts.append(f"{label}: {ins[key]}")
    return "\n".join(parts)


class UserSimulator:
    def __init__(self, scenario: str, model: str | None = None, client: anthropic.Anthropic | None = None):
        self.model = model or os.getenv("SIM_MODEL", "claude-haiku-5-5")
        self.client = client or anthropic.Anthropic(max_retries=8)
        self.system = GUIDELINES + "\n<scenario>\n" + scenario + "\n</scenario>"
        self.messages: list[dict] = []  # roles flipped: agent = "user", customer (us) = "assistant"
        self.usage = Usage()

    def reply(self, agent_text: str) -> str:
        self.messages.append({"role": "user", "content": agent_text or "(no message)"})
        started = time.perf_counter()
        resp = self.client.messages.create(
            model=self.model, max_tokens=4000, system=self.system, messages=self.messages,
            output_config={"effort": "low"},
        )
        self.usage.add(self.model, resp.usage, round((time.perf_counter() - started) * 1000))
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        self.messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True)
                                                               for b in resp.content]})
        return text or OUT_OF_SCOPE

    @staticmethod
    def ended(text: str) -> str | None:
        return next((t for t in END_TOKENS if t in text), None)
