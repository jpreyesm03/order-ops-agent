"""Router + specialist sub-agents (the alternative to one agent with every tool).

    customer message ─▶ router (cheap call, structured output) ─▶ one specialist
                                                                    │ own context, own tool subset,
                                                                    │ own slice of the policy
                                                                    └─▶ handoff(to=...) if it's the wrong one

Each specialist keeps its own history (context isolation). Turns handled by another specialist are passed in as a
short text recap, so it never sees the other specialists' tool calls. The policy gate is the same for all of them.
"""
from __future__ import annotations

import json
import os
import re
import time

import anthropic

from . import db, tracing
from .agent import PREAMBLE, Agent, Turn, Usage, record_llm
from .gate import Gate, Session
from .tool_specs import TOOLS_BY_NAME

READS = ["find_user_id_by_email", "find_user_id_by_name_zip", "get_user_details", "get_order_details",
         "get_product_details", "get_item_details", "list_all_product_types", "calculate", "transfer_to_human_agents"]

SPECIALISTS = {
    "orders": {
        "about": "pending orders: cancel, change shipping address, change payment method, change item options; "
                 "order status and tracking questions",
        "writes": ["cancel_pending_order", "modify_pending_order_address", "modify_pending_order_payment",
                   "modify_pending_order_items"],
        "sections": ["Cancel pending order", "Modify pending order"],
    },
    "returns": {
        "about": "delivered orders: return items or exchange them for other options of the same product",
        "writes": ["return_delivered_order_items", "exchange_delivered_order_items"],
        "sections": ["Return delivered order", "Exchange delivered order"],
    },
    "account": {
        "about": "the customer's profile: default address, payment methods, finding their orders, product questions",
        "writes": ["modify_user_address"],
        "sections": [],
    },
}

HANDOFF_TOOL = {
    "name": "handoff", "strict": True,
    "description": "Pass the conversation to the right specialist when the customer's request is outside your area. "
                   "Use it instead of saying you can't help.",
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["to", "reason"],
                     "properties": {"to": {"type": "string", "enum": list(SPECIALISTS)},
                                    "reason": {"type": "string"}}},
}


def split_policy(text: str) -> tuple[str, dict[str, str]]:
    """Common part (everything before the action sections) + one entry per '## <action>' section."""
    parts = re.split(r"(?m)^## ", text)
    common, sections = [parts[0]], {}
    for p in parts[1:]:
        title = p.splitlines()[0].strip()
        if title in ("Domain basic", "Generic action rules"):
            common.append("## " + p)
        else:
            sections[title] = "## " + p
    return "".join(common), sections


def specialist_prompt(name: str, policy: str | None = None) -> str:
    common, sections = split_policy(policy or (db.TAU2_DIR / "policy.md").read_text())
    spec = SPECIALISTS[name]
    others = "\n".join(f"- {k}: {v['about']}" for k, v in SPECIALISTS.items() if k != name)
    body = "\n\n".join(sections[s] for s in spec["sections"])
    return (PREAMBLE + f"You are the **{name}** specialist. Your area: {spec['about']}.\n"
            f"Other specialists (use the handoff tool to reach them):\n{others}\n\n" + common + "\n\n" + body)


ROUTER_SYSTEM = """You route a retail customer-service chat to one specialist.
Specialists:
""" + "\n".join(f"- {k}: {v['about']}" for k, v in SPECIALISTS.items()) + """
Stay with the current specialist unless the customer's new message is clearly about another area.
Authentication and greetings go to the current specialist (or 'orders' at the start)."""


def route(client: anthropic.Anthropic, model: str, current: str, transcript: list[str], text: str,
          usage: Usage) -> str:
    """One cheap structured-output call: which specialist takes the customer's new message."""
    recent = "\n".join(transcript[-6:])
    with tracing.span("router", "LLM", **{"gen_ai.system": "anthropic", "gen_ai.request.model": model,
                                          "order_ops.router.current": current}) as sp:
        started = time.perf_counter()
        resp = client.messages.create(
            model=model, max_tokens=1000, system=ROUTER_SYSTEM,
            messages=[{"role": "user", "content": f"Current specialist: {current}\n\nRecent conversation:\n"
                                                  f"{recent or '(start)'}\n\nNew customer message:\n{text}"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": {
                "type": "object", "additionalProperties": False, "required": ["specialist"],
                "properties": {"specialist": {"type": "string", "enum": list(SPECIALISTS)}}}}},
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        usage.add(model, resp.usage, latency_ms)
        record_llm(sp, model, resp.usage, latency_ms, resp.stop_reason)
        choice = json.loads(next(b.text for b in resp.content if b.type == "text"))["specialist"]
        sp.set_attribute("order_ops.router.choice", choice)
        return choice


class RoutedAgent:
    def __init__(self, gate: Gate, client: anthropic.Anthropic | None = None, model: str | None = None,
                 router_model: str | None = None):
        self.gate = gate
        self.client = client or anthropic.Anthropic(max_retries=8)
        self.model = model or os.getenv("AGENT_MODEL", "claude-haiku-5-5")
        self.router_model = router_model or os.getenv("ROUTER_MODEL", "claude-haiku-5-5")
        self.agents = {
            name: Agent(gate, model=self.model, client=self.client, system=specialist_prompt(name), name=name,
                        tools=[TOOLS_BY_NAME[t] for t in READS + spec["writes"]] + [HANDOFF_TOOL])
            for name, spec in SPECIALISTS.items()
        }
        self.histories: dict[str, list[dict]] = {name: [] for name in SPECIALISTS}
        self.seen: dict[str, int] = {name: 0 for name in SPECIALISTS}  # transcript lines each specialist has seen
        self.transcript: list[str] = []
        self.current = "orders"
        self.router_usage = Usage()
        self.routes: list[str] = []
        self.handoffs = 0

    @property
    def usage(self) -> Usage:
        total = Usage()
        total.merge(self.router_usage)
        for a in self.agents.values():
            total.merge(a.usage)
        return total

    def cost(self) -> float:
        return self.router_usage.cost(self.router_model) + sum(a.usage.cost(a.model) for a in self.agents.values())

    def _route(self, text: str) -> str:
        return route(self.client, self.router_model, self.current, self.transcript, text, self.router_usage)

    def _recap(self, name: str) -> str:
        missed = self.transcript[self.seen[name]:-1]  # everything except the new customer line
        if not missed:
            return ""
        return "[Earlier in this conversation, handled by a colleague]\n" + "\n".join(missed) + \
               "\n[End of recap. The customer's new message follows.]\n\n"

    def respond(self, session: Session, _messages: list[dict], customer_text: str,
                notices: list[str] | None = None) -> Turn:
        self.transcript.append(f"Customer: {customer_text}")
        name = self._route(customer_text)
        self.routes.append(name)
        calls: list[dict] = []
        for hop in range(3):  # at most two handoffs per customer turn
            self.current = name
            agent = self.agents[name]
            prefix = self._recap(name)
            turn = agent.respond(session, self.histories[name], prefix + customer_text, notices=notices,
                                 gate_text=customer_text, new_customer_turn=(hop == 0))
            self.seen[name] = len(self.transcript)
            calls += [c for c in turn.tool_calls if c["name"] != "handoff"]
            handoff = next((c for c in turn.tool_calls if c["name"] == "handoff"), None)
            if handoff is None:
                self.transcript.append(f"Agent: {turn.reply}")
                self.seen[name] = len(self.transcript)
                return Turn(turn.reply, calls, turn.transferred)
            self.handoffs += 1
            name = handoff["args"]["to"]
            notices = None
        reply = turn.reply or "Let me get you to a human agent who can help with this."
        self.transcript.append(f"Agent: {reply}")
        return Turn(reply, calls, turn.transferred)
