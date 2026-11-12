"""Adapter: this repo's agent (single or routed, D11) inside τ²-bench's own harness (D21).

τ²'s orchestrator owns the conversation: its user simulator writes the customer's messages, its environment runs
the tools on its own in-memory database, and its evaluator grades the final database by replaying the tool calls
in the transcript. So every tool call the gate allows must go out to τ² as a visible tool call:

    customer message ─▶ P5Tau2Agent.generate_next_message ─▶ model (same prompt, tools, loop rules as the app)
                                                               │ tool call
                                                               ▼
                                                   Tau2Gate.review (same rule code as the app's gate, D23)
                                     blocked ◀──────────────┤────────────▶ allowed
               (result goes straight back to the model;     │             returned to τ² as a ToolCall;
                τ² never sees the call)                     │             τ² runs it, sends the ToolMessage back,
                                                            │             Tau2Gate.after updates the session
Gate modes: "on" blocks; "off" lets everything through but records what it would have blocked (shadow mode).

Only importable with the eval dependency group:  uv sync --group tau2
"""
from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import db


def _data_dir() -> Path:
    """τ² reads its data from TAU2_DATA_DIR/tau2/domains/retail. Point it at this repo's pinned copy (same commit)."""
    root = db.ROOT / ".cache" / "tau2_data"
    retail = root / "tau2" / "domains" / "retail"
    retail.mkdir(parents=True, exist_ok=True)
    for name in ("db.json", "policy.md", "tasks.json", "split_tasks.json"):
        link = retail / name
        if not link.exists():
            link.symlink_to(db.TAU2_DIR / name)
    english = root / "tau2" / "multilingual" / "en"  # τ²'s standard English run: reviewed task wording + personas
    if not english.exists():
        english.parent.mkdir(parents=True, exist_ok=True)
        english.symlink_to(db.TAU2_DIR / "multilingual" / "en")
    return root


os.environ.setdefault("TAU2_DATA_DIR", str(_data_dir()))

import anthropic  # noqa: E402
from tau2.agent.base_agent import HalfDuplexAgent  # noqa: E402
from tau2.data_model.message import (  # noqa: E402
    AssistantMessage,
    Message,
    MultiToolMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)

from . import confirm, tracing  # noqa: E402
from .agent import MAX_STEPS, PREAMBLE, Usage, make_agent, with_summary  # noqa: E402
from .gate import AUTH_TOOLS, GATE_TOOLS, Blocked, Gate, Session, record_outcome  # noqa: E402
from .router import HANDOFF_TOOL, READS, SPECIALISTS, route, specialist_prompt  # noqa: E402
from .store import ALL_TOOLS, WRITE_TOOLS, ToolError  # noqa: E402
from .tool_specs import TOOLS, TOOLS_BY_NAME  # noqa: E402

THRESHOLD = float(os.getenv("APPROVAL_THRESHOLD_USD", "250"))
GIVE_UP = "I'm sorry, I couldn't finish that. Let me transfer you to a human agent."


class Tau2Gate(Gate):
    """The app's gate, reading τ²'s in-memory retail database and keeping its records in memory."""

    def __init__(self, retail_db: Any, enforce: bool = True):  # no Postgres: every lookup below is overridden
        self.tdb = retail_db
        self.enforce = enforce
        self.done_keys: set[str] = set()
        self.audit: list[dict] = []

    # ---------- lookups ----------
    def _order_owner(self, order_id: str) -> str | None:
        o = self.tdb.orders.get(order_id)
        return None if o is None else o.user_id

    def _owns_payment_method(self, user_id: str | None, payment_method_id: str) -> bool:
        u = self.tdb.users.get(user_id) if user_id else None
        return u is not None and payment_method_id in u.payment_methods

    def _order(self, order_id: str) -> dict:
        o = self.tdb.orders.get(order_id)
        if o is None:
            raise ToolError("Order not found")
        return o.model_dump(mode="json")

    def _variant_price(self, item_id: str) -> Decimal | None:
        for p in self.tdb.products.values():
            if item_id in p.variants:
                return Decimal(str(p.variants[item_id].price))
        return None

    def _audit_raw(self, session_id: str, tool: str, tier: str, args: dict, outcome: str, detail: str) -> None:
        self.audit.append({"tool": tool, "tier": tier, "outcome": outcome, "detail": detail})

    def _already_done(self, session: Session, tool: str, args: dict[str, Any]) -> bool:
        return self._key(session.session_id, tool, args) in self.done_keys

    def _variant_info(self, item_id: str) -> dict | None:
        for p in self.tdb.products.values():
            if item_id in p.variants:
                v = p.variants[item_id].model_dump(mode="json")
                return {"item_id": item_id, "options": v.get("options"), "available": v.get("available"),
                        "price": v.get("price")}
        return None

    def gate_tool(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "find_variants":
            p = self.tdb.products.get(args.get("product_id", ""))
            if p is None:
                raise ToolError("Product not found")
            return confirm.rank_variants(p.model_dump(mode="json"), args.get("wanted_options") or {})
        raise ToolError(f"Unknown tool {tool}")

    # ---------- the two halves of Gate.run, around τ² executing the call ----------
    def _stop(self, session: Session, rule: str, message: str) -> dict | None:
        if self.enforce:
            session.stats["blocked"][rule] = session.stats["blocked"].get(rule, 0) + 1
            return {"ok": False, "blocked_by_policy": rule, "error": message}
        would = session.stats.setdefault("would_block", {})
        would[rule] = would.get(rule, 0) + 1
        return None

    def review(self, session: Session, tool: str, args: dict[str, Any]) -> dict | None:
        """Before τ² runs a call. Returns the reply for the model when the gate stops it; None lets τ² run it."""
        tier = "write" if tool in WRITE_TOOLS else "read"
        with tracing.span("tool", "TOOL", **{"tool.name": tool, "order_ops.tier": tier,
                                             "order_ops.order_id": args.get("order_id"),
                                             "order_ops.gate.enforce": self.enforce}) as sp:
            out = self._review(session, tool, args, tier)
            if out is None:
                sp.set_attribute("order_ops.outcome", "forwarded")
            else:
                record_outcome(sp, out)
            return out

    def _review(self, session: Session, tool: str, args: dict[str, Any], tier: str) -> dict | None:
        if tool in GATE_TOOLS:  # answered here; τ² never sees it (read-only)
            try:
                result = self.gate_tool(tool, args)
            except ToolError as e:
                return {"ok": False, "error": str(e)}
            session.remember(json.dumps(result, default=str))
            return {"ok": True, "result": result}
        if tool not in ALL_TOOLS:
            return None  # τ² answers unknown tools itself
        try:
            self._check(session, tool, args)
        except Blocked as b:
            self._audit(session, tool, tier, args, "blocked" if self.enforce else "would_block", b.rule)
            stopped = self._stop(session, b.rule, str(b))
            if stopped is not None:
                return stopped
        try:
            if tool in WRITE_TOOLS:
                if self._key(session.session_id, tool, args) in self.done_keys:
                    session.stats["duplicates"] += 1
                    self._audit(session, tool, tier, args, "duplicate", "")
                    if self.enforce:
                        return {"ok": True, "duplicate": True,
                                "note": "This exact change was already applied; nothing was done again."}
                    return None
                proposal = self.propose(session, tool, args)
                if proposal is not None:
                    return proposal
                if self.amount_at_stake(tool, args) > Decimal(str(session.approval_threshold)):
                    session.stats["approvals"] += 1  # eval mode: counted, then executed (as in the app's evals)
        except (ToolError, KeyError, TypeError):
            return None  # bad arguments: τ²'s tool reports the error, as it would without the gate
        return None

    def after(self, session: Session, tool: str, args: dict[str, Any], content: str | None,
              error: bool) -> dict:
        """After τ² ran the call: update the session as Gate.run would; returns the result in the app's format."""
        if error:
            session.stats["errors"] += 1
            return {"ok": False, "error": content or "error"}
        session.remember(content or "")
        try:
            result: Any = json.loads(content) if content else content
        except json.JSONDecodeError:
            result = content
        if tool in AUTH_TOOLS:
            try:
                self._authenticate(session, result)
            except Blocked as b:
                stopped = self._stop(session, b.rule, str(b))
                if stopped is not None:
                    return stopped
        if tool in WRITE_TOOLS:
            self.done_keys.add(self._key(session.session_id, tool, args))
            session.confirmation = False  # one "yes" pays for one successful change
            session.confirmed = None
            session.stats["writes"] += 1
            session.note_done(tool, args)
        return {"ok": True, "result": result}


def _retail_db(tools: list) -> Any:
    """The database behind τ²'s tools: they are bound methods of the environment's RetailTools."""
    return tools[0]._func.__self__.db


class P5Tau2Agent(HalfDuplexAgent[dict]):
    """This repo's agent as a τ² half-duplex agent. One instance per simulation (τ²'s factory builds one per task)."""

    def __init__(self, tools: list, domain_policy: str, *, arch: str = "single", gate: str = "on",
                 model: str | None = None, effort: str | None = None, router_model: str | None = None,
                 client: anthropic.Anthropic | None = None, language_guidance: str = "",
                 task_id: str | None = None):
        super().__init__(tools=tools, domain_policy=domain_policy)
        if arch not in ("single", "routed") or gate not in ("on", "off"):
            raise ValueError(f"arch={arch} gate={gate}")
        self.arch, self.gate_mode = arch, gate
        self.client = client or anthropic.Anthropic(max_retries=8)
        self.gate = Tau2Gate(_retail_db(tools), enforce=(gate == "on"))
        self.session = Session(session_id=f"tau2-{uuid.uuid4().hex[:10]}", auto_approve=True,
                               approval_threshold=THRESHOLD)
        policy = domain_policy + (f"\n\n{language_guidance}" if language_guidance else "")
        if arch == "single":
            self.workers = {"agent": make_agent(self.gate, model=model, effort=effort, tools=TOOLS,
                                                system=PREAMBLE + policy, client=self.client, name="agent")}
            self.current = "agent"
        else:
            self.workers = {name: make_agent(self.gate, model=model, effort=effort, client=self.client, name=name,
                                             system=specialist_prompt(name, policy),
                                             tools=[TOOLS_BY_NAME[t] for t in READS + spec["writes"]] + [HANDOFF_TOOL])
                            for name, spec in SPECIALISTS.items()}
            self.current = "orders"
        self.router_model = router_model or os.getenv("ROUTER_MODEL", "claude-haiku-5-5")
        self.router_usage = Usage()
        self.histories: dict[str, list[dict]] = {n: [] for n in self.workers}
        self.seen = {n: 0 for n in self.workers}
        self.transcript: list[str] = []
        self.routes: list[str] = []
        self.handoffs = 0
        self.pending: dict[str, tuple[str, dict]] = {}
        self.tool_calls: list[dict] = []
        self.customer_text = ""
        self.steps = 0
        self.turn_no = 0
        self.trace_ids = tracing.start_conversation(
            self.session.session_id, **{"order_ops.channel": "tau2", "order_ops.arch": arch,
                                        "order_ops.gate": gate, "order_ops.task_id": task_id})

    # ---------- τ² interface ----------
    def get_init_state(self, message_history: list[Message] | None = None) -> dict:
        return {}

    def generate_next_message(self, message: Any, state: dict) -> tuple[AssistantMessage, dict]:
        with tracing.in_conversation(self.trace_ids), tracing.span(
                "agent.step", "AGENT", **{"order_ops.arch": self.arch, "order_ops.gate": self.gate_mode}) as sp:
            if isinstance(message, UserMessage):
                self._on_customer(message.content or "")
                sp.set_attribute("order_ops.turn", self.turn_no)
            elif isinstance(message, (ToolMessage, MultiToolMessage)):
                self._on_tool_results(message.tool_messages if isinstance(message, MultiToolMessage) else [message])
            out = self._step()
            sp.set_attribute("order_ops.step.kind", "tool_call" if out.tool_calls else "reply")
            return out, state

    # ---------- internals ----------
    def _on_customer(self, text: str) -> None:
        self.turn_no += 1
        self.steps = 0
        self.customer_text = text
        self.session.on_customer_message(text)
        if self.arch == "single":
            self.histories["agent"].append({"role": "user", "content": text})
            return
        self.transcript.append(f"Customer: {text}")
        name = route(self.client, self.router_model, self.current, self.transcript, text, self.router_usage)
        self.routes.append(name)
        self._hand_to(name)

    def _hand_to(self, name: str) -> None:
        """Routed: give the customer's message to a specialist, with a recap of what it missed."""
        self.current = name
        missed = self.transcript[self.seen[name]:-1]
        recap = ("[Earlier in this conversation, handled by a colleague]\n" + "\n".join(missed)
                 + "\n[End of recap. The customer's new message follows.]\n\n") if missed else ""
        self.histories[name].append({"role": "user", "content": recap + self.customer_text})
        self.seen[name] = len(self.transcript)

    def _on_tool_results(self, msgs: list[ToolMessage]) -> None:
        blocks = []
        for tm in msgs:
            tool, args = self.pending.pop(tm.id, ("unknown", {}))
            out = self.gate.after(self.session, tool, args, tm.content, tm.error)
            self.tool_calls.append({"name": tool, "args": args, "result": out, "ran_in_tau2": True})
            blocks.append(_tool_result(tm.id, out))
        self.histories[self.current].append({"role": "user", "content": blocks})

    def _reply(self, text: str) -> AssistantMessage:
        text = with_summary(self.session, text)
        if self.arch == "routed":
            self.transcript.append(f"Agent: {text}")
            self.seen[self.current] = len(self.transcript)
        return AssistantMessage(role="assistant", content=text)

    def _step(self) -> AssistantMessage:
        hops = 0
        while self.steps < MAX_STEPS:
            self.steps += 1
            worker, history = self.workers[self.current], self.histories[self.current]
            resp = worker._create(history)
            history.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            if resp.stop_reason == "refusal":
                return self._reply("Sorry, I can't help with that request.")
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                return self._reply("".join(b.text for b in resp.content if b.type == "text").strip())
            tu = tool_uses[0]  # parallel tool use is disabled in the request
            args = dict(tu.input)
            if tu.name == "handoff":
                history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu.id,
                                                             "content": "Handed off."}]})
                hops += 1
                self.handoffs += 1
                if hops > 2 or args.get("to") not in self.workers:
                    return self._reply("Let me get you to a human agent who can help with this.")
                self._hand_to(args["to"])
                continue
            stopped = self.gate.review(self.session, tu.name, args)
            if stopped is not None:
                self.tool_calls.append({"name": tu.name, "args": args, "result": stopped, "ran_in_tau2": False})
                history.append({"role": "user", "content": [_tool_result(tu.id, stopped)]})
                continue
            self.pending[tu.id] = (tu.name, args)
            return AssistantMessage(role="assistant", content=None,
                                    tool_calls=[ToolCall(id=tu.id, name=tu.name, arguments=args,
                                                         requestor="assistant")])
        return self._reply(GIVE_UP)

    # ---------- results ----------
    @property
    def usage(self) -> Usage:
        total = Usage()
        total.merge(self.router_usage)
        for w in self.workers.values():
            total.merge(w.usage)
        return total

    def cost(self) -> float:
        return self.router_usage.cost(self.router_model) + sum(w.usage.cost(w.model) for w in self.workers.values())

    def close(self) -> None:
        for w in self.workers.values():
            w.close()

    def report(self) -> dict[str, Any]:
        return {"arch": self.arch, "gate": self.gate_mode, "gate_stats": self.session.stats,
                "usage": vars(self.usage), "agent_cost_usd": round(self.cost(), 5),
                "model": next(iter(self.workers.values())).model, "routes": self.routes or None,
                "handoffs": self.handoffs, "tool_calls": self.tool_calls,
                "trace_id": (self.trace_ids or {}).get("trace_id")}


def _tool_result(tool_use_id: str, out: dict) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(out, default=str),
            "is_error": not out.get("ok", False)}
