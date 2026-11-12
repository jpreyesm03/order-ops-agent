"""Policy gate: the rules a jailbroken model still cannot break, because they run in code, not in the prompt.

Every tool call from the model goes through `Gate.run`:
  1. tier          read / write (store.READ_TOOLS, store.WRITE_TOOLS)
  2. identity      user/order data and every write need an authenticated customer (found by email or name+zip)
  3. one customer  after authentication, lookups that resolve to a different customer are blocked
  4. ownership     orders, users and payment methods must belong to the authenticated customer
  5. confirmation  a write first returns a summary rendered by code from its exact arguments (shown to the
                   customer); it runs only when called again with the same arguments after the customer's explicit
                   "yes" to that summary (D31)
  5b. provenance   addresses, payment methods and item ids in a write must come from the customer's words or from
                   tool results of this conversation (D31)
  6. approval      money movements above the threshold wait for a human (live) or are counted (eval)
  7. idempotency   the same write with the same arguments in the same session runs once
  8. audit         every call is logged with its outcome

With `enforce=False` (shadow mode, for measuring the gate) rules 2-6 only count what they would have blocked
(`stats["would_block"]`) and the call runs as the model asked. Idempotency stays on: it protects the data, and a
repeated write is reported as `duplicates` either way.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
from opentelemetry import trace
from psycopg.types.json import Jsonb

from . import confirm, db, tracing
from .store import READ_TOOLS, WRITE_TOOLS, Store, ToolError, call

AUTH_TOOLS = {"find_user_id_by_email", "find_user_id_by_name_zip"}
GATE_TOOLS = {"find_variants"}  # answered by the gate itself from the store's data (D31)
SEEN_LIMIT = 400_000  # characters of conversation data kept for the provenance check
NEEDS_AUTH = {"get_user_details", "get_order_details"} | WRITE_TOOLS

_YES = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|correct|proceed|go ahead|do it|please do|that'?s right|"
    r"sounds good|that works|absolutely|affirmative|si|sí)\b",
    re.IGNORECASE,
)
_NO_START = re.compile(r"^\W*(no|nope|don'?t|do not|wait|hold on|not yet|actually,? no)\b", re.IGNORECASE)


def is_confirmation(text: str) -> bool:
    """Deterministic check for an explicit yes. Errs on the side of 'not confirmed'."""
    return bool(_YES.search(text)) and not _NO_START.search(text)


@dataclass
class Session:
    session_id: str
    user_id: str | None = None            # authenticated customer
    confirmation: bool = False            # the customer's latest message is an explicit "yes" (shadow mode check)
    auto_approve: bool = False            # eval mode: approvals are counted, then executed
    approval_threshold: float = 250.0
    stats: dict[str, Any] = field(default_factory=lambda: {"blocked": {}, "approvals": 0, "duplicates": 0,
                                                          "writes": 0, "errors": 0, "proposals": 0})
    pending: dict | None = None           # proposal shown to the customer: {"tool", "fp", "summary"}
    confirmed: str | None = None          # fingerprint of the proposal the customer said yes to
    new_summary: str | None = None        # summary created in this turn, still to be shown to the customer
    new_done: list = field(default_factory=list)  # "Done: ..." lines for changes that ran this turn
    seen: str = ""                        # customer messages + tool results of this conversation (provenance)

    def on_customer_message(self, text: str) -> None:
        self.confirmation = is_confirmation(text)
        self.remember(text)
        if self.pending is None:
            return
        if self.confirmation:
            self.confirmed, self.pending = self.pending["fp"], None
        elif _NO_START.search(text):
            self.pending = None  # declined; a new proposal is needed

    def remember(self, text: str) -> None:
        self.seen = (self.seen + "\n" + str(text))[-SEEN_LIMIT:]

    def note_done(self, tool: str, args: dict, pending_approval: bool = False) -> None:
        self.new_done.append(confirm.done(tool, args, pending_approval))

    def take_summary(self) -> str | None:
        """What code adds to this turn's reply (once): "Done" lines for changes that ran, then the new summary."""
        parts = [*self.new_done, *([self.new_summary] if self.new_summary else [])]
        self.new_done, self.new_summary = [], None
        return "\n\n".join(parts) or None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> Session:
        return cls(**d)


def outcome(out: dict[str, Any]) -> str:
    if out.get("blocked_by_policy"):
        return "blocked"
    if out.get("pending_approval"):
        return "pending_approval"
    if out.get("duplicate"):
        return "duplicate"
    return "ok" if out.get("ok") else "error"


def record_outcome(sp: Any, out: dict[str, Any]) -> None:
    """The gate's decision on the tool span: outcome, and the rule and reason when it blocked."""
    tracing.set_attrs(sp, **{"order_ops.outcome": outcome(out), "order_ops.gate.rule": out.get("blocked_by_policy"),
                             "order_ops.approval_id": out.get("approval_id")})
    if out.get("blocked_by_policy"):
        sp.add_event("gate.blocked", {"rule": out["blocked_by_policy"], "reason": out.get("error", "")})


class Blocked(Exception):
    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule


class Gate:
    def __init__(self, conn: psycopg.Connection, schema: str, store: Store | None = None, enforce: bool = True):
        self.conn = conn
        self.enforce = enforce
        self.s = db.ident(schema)
        self.store = store or Store(conn, schema)  # ShopifyStore for the live Shopify demo

    # ---------- public ----------
    def run(self, session: Session, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute one model tool call under the policy. Always returns a JSON-able dict for the model."""
        tier = "write" if tool in WRITE_TOOLS else "read"
        with tracing.span("tool", "TOOL", **{"tool.name": tool, "order_ops.tier": tier,
                                             "order_ops.order_id": args.get("order_id")}) as sp:
            out = self._run(session, tool, args, tier)
            record_outcome(sp, out)
            return out

    def _run(self, session: Session, tool: str, args: dict[str, Any], tier: str) -> dict[str, Any]:
        try:
            if tool not in READ_TOOLS | WRITE_TOOLS | GATE_TOOLS:
                raise ToolError(f"Unknown tool {tool}")
            if tool in GATE_TOOLS:
                result = self.gate_tool(tool, args)
                session.remember(json.dumps(result, default=str))
                self._audit(session, tool, tier, args, "ok")
                return {"ok": True, "result": result}
            try:
                self._check(session, tool, args)
            except Blocked as b:
                if self.enforce:
                    raise
                self._shadow(session, tool, tier, args, b.rule)
            if tool in WRITE_TOOLS:
                if not self._already_done(session, tool, args):
                    proposal = self.propose(session, tool, args)
                    if proposal is not None:
                        return proposal
                return self._write(session, tool, args)
            result = call(self.store, tool, args)
            session.remember(json.dumps(result, default=str))
            if tool in AUTH_TOOLS:
                try:
                    self._authenticate(session, result)
                except Blocked as b:
                    if self.enforce:
                        raise
                    self._shadow(session, tool, tier, args, b.rule)  # stays the first customer, as if enforced
            self._audit(session, tool, tier, args, "ok")
            return {"ok": True, "result": result}
        except Blocked as b:
            session.stats["blocked"][b.rule] = session.stats["blocked"].get(b.rule, 0) + 1
            self._audit(session, tool, tier, args, "blocked", f"{b.rule}: {b}")
            return {"ok": False, "blocked_by_policy": b.rule, "error": str(b)}
        except ToolError as e:
            session.stats["errors"] += 1
            self._audit(session, tool, tier, args, "error", str(e))
            return {"ok": False, "error": str(e)}
        except psycopg.Error as e:  # never crash the conversation on a database error; the write rolled back
            session.stats["errors"] += 1
            self._audit(session, tool, tier, args, "error", f"database: {type(e).__name__}")
            return {"ok": False, "error": "A temporary problem occurred and nothing was changed. Try again."}

    def decide(self, approval_id: str, approve: bool, reviewer: str, note: str = "") -> dict[str, Any]:
        """A human approves or rejects a pending money movement (live app)."""
        with tracing.span("approval.decision", "CHAIN", **{"order_ops.approval_id": approval_id,
                                                           "order_ops.approved": approve}) as sp:
            out = self._decide(approval_id, approve, reviewer, note)
            sp.set_attribute("order_ops.approval.status", out["status"])
            return out

    def _decide(self, approval_id: str, approve: bool, reviewer: str, note: str) -> dict[str, Any]:
        row = self.conn.execute(
            f"SELECT session_id, tool, args, status FROM {self.s}.approvals WHERE approval_id = %s", (approval_id,)
        ).fetchone()
        if row is None:
            raise ToolError("Approval not found")
        session_id, tool, args, status = row
        if status != "pending":
            raise ToolError(f"Approval already {status}")
        if not approve:
            self.conn.execute(
                f"UPDATE {self.s}.approvals SET status='rejected', decided_by=%s, decision_note=%s, decided_at=now() "
                f"WHERE approval_id=%s", (reviewer, note, approval_id))
            return {"status": "rejected"}
        try:
            result = self._execute_once(f"approval:{approval_id}", tool, args)
            status, payload = "approved", result
        except ToolError as e:  # data changed since the request (e.g. order shipped meanwhile)
            status, payload = "failed", {"error": str(e)}
        self.conn.execute(
            f"UPDATE {self.s}.approvals SET status=%s, decided_by=%s, decision_note=%s, decided_at=now(), result=%s "
            f"WHERE approval_id=%s", (status, reviewer, note, Jsonb(payload), approval_id))
        self._audit_raw(session_id, tool, "write", args, "ok" if status == "approved" else "error",
                        f"approval {approval_id} {status} by {reviewer}")
        return {"status": status, "result": payload}

    # ---------- lookups (the only data the rules read; Tau2Gate answers them from τ²'s own database) ----------
    def _order_owner(self, order_id: str) -> str | None:
        row = self.conn.execute(f"SELECT user_id FROM {self.s}.orders WHERE order_id = %s", (order_id,)).fetchone()
        return None if row is None else row[0]

    def _owns_payment_method(self, user_id: str | None, payment_method_id: str) -> bool:
        return self.conn.execute(
            f"SELECT 1 FROM {self.s}.payment_methods WHERE user_id = %s AND payment_method_id = %s",
            (user_id, payment_method_id)).fetchone() is not None

    def _order(self, order_id: str) -> dict:
        """The order as the tools return it (items with prices, payment_history)."""
        return self.store._order(order_id)

    def _variant_price(self, item_id: str) -> Decimal | None:
        row = self.conn.execute(f"SELECT price FROM {self.s}.variants WHERE item_id = %s LIMIT 1",
                                (item_id,)).fetchone()
        return None if row is None else row[0]

    def _shadow(self, session: Session, tool: str, tier: str, args: dict, rule: str) -> None:
        """Shadow mode: count the call the gate would have stopped, then let it run."""
        would = session.stats.setdefault("would_block", {})
        would[rule] = would.get(rule, 0) + 1
        self._audit(session, tool, tier, args, "would_block", rule)

    # ---------- checks ----------
    def _check(self, session: Session, tool: str, args: dict[str, Any]) -> None:
        if tool in NEEDS_AUTH and session.user_id is None:
            raise Blocked("identity", "Authenticate the customer first (find_user_id_by_email or "
                                      "find_user_id_by_name_zip) before accessing account data or making changes.")
        if tool == "get_user_details" or tool == "modify_user_address":
            if args.get("user_id") != session.user_id:
                raise Blocked("ownership", "You can only access the authenticated customer's own profile.")
        if "order_id" in args and tool in NEEDS_AUTH:
            owner = self._order_owner(args["order_id"])
            if owner is not None and owner != session.user_id:
                raise Blocked("ownership", "This order belongs to a different customer. You can only help the "
                                           "authenticated customer with their own orders.")
        if "payment_method_id" in args and tool in WRITE_TOOLS:
            if not self._owns_payment_method(session.user_id, args["payment_method_id"]):
                raise Blocked("ownership", "That payment method does not belong to the authenticated customer.")
        if tool in WRITE_TOOLS:
            missing = confirm.unsourced(args, session.seen)
            if missing:
                raise Blocked("provenance", "These values didn't come from the customer or from account data you "
                                            "looked up in this conversation: " + ", ".join(missing) + ". Ask the "
                                            "customer for them or look them up; never fill them in yourself.")
            if not self.enforce and not session.confirmation:
                raise Blocked("confirmation", "No explicit yes from the customer for this change.")

    # ---------- confirmation (D31) ----------
    def propose(self, session: Session, tool: str, args: dict[str, Any]) -> dict | None:
        """None if the customer said yes to exactly this call; otherwise the summary they must confirm."""
        if not self.enforce:
            return None  # shadow mode: the call runs as asked (the confirmation rule was counted in _check)
        fp = confirm.fingerprint(tool, args)
        if session.confirmed == fp:
            return None
        order = None
        if args.get("order_id"):
            try:
                order = self._order(args["order_id"])
            except ToolError:
                order = None
        try:
            approval = self.amount_at_stake(tool, args) > Decimal(str(session.approval_threshold))
        except (ToolError, KeyError, TypeError):
            approval = False
        text = confirm.summary(tool, args, order, self._variant_info, needs_approval=approval)
        session.pending = {"tool": tool, "fp": fp, "summary": text}
        session.new_summary = text
        session.stats["proposals"] = session.stats.get("proposals", 0) + 1
        return {"ok": False, "needs_confirmation": True, "summary": text,
                "instruction": "Nothing was changed yet. This summary is shown to the customer. Wait for their "
                               "explicit yes, then call this tool again with exactly the same arguments. If they "
                               "want something different, call the tool with the new details instead."}

    def _already_done(self, session: Session, tool: str, args: dict[str, Any]) -> bool:
        key = self._key(session.session_id, tool, args)
        return self.conn.execute(f"SELECT 1 FROM {self.s}.idempotency_keys WHERE key = %s",
                                 (key,)).fetchone() is not None

    def _variant_info(self, item_id: str) -> dict | None:
        try:
            return call(self.store, "get_item_details", {"item_id": item_id})
        except ToolError:
            return None

    def gate_tool(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "find_variants":
            product = call(self.store, "get_product_details", {"product_id": args.get("product_id", "")})
            return confirm.rank_variants(product, args.get("wanted_options") or {})
        raise ToolError(f"Unknown tool {tool}")

    def _authenticate(self, session: Session, user_id: str) -> None:
        if session.user_id is not None and session.user_id != user_id:
            raise Blocked("one_customer", "This conversation is already authenticated as another customer. "
                                          "Only one customer can be helped per conversation.")
        session.user_id = user_id

    # ---------- writes ----------
    def amount_at_stake(self, tool: str, args: dict[str, Any]) -> Decimal:
        """Money that moves if this write runs (refund, charge or transfer between payment methods)."""
        if tool in ("modify_pending_order_address", "modify_user_address"):
            return Decimal("0")
        order = self._order(args["order_id"])
        if tool in ("cancel_pending_order", "modify_pending_order_payment"):
            return sum((Decimal(str(p["amount"])) for p in order["payment_history"]
                        if p["transaction_type"] == "payment"), Decimal("0"))
        prices = {it["item_id"]: Decimal(str(it["price"])) for it in order["items"]}
        if tool == "return_delivered_order_items":
            return sum((prices.get(i, Decimal("0")) for i in args.get("item_ids", [])), Decimal("0"))
        # exchange / modify items: the net price difference charged or refunded
        diff = Decimal("0")
        for old, new in zip(args.get("item_ids", []), args.get("new_item_ids", []), strict=False):
            price = self._variant_price(new)
            if price is not None:
                diff += price - prices.get(old, Decimal("0"))
        return abs(diff)

    def _write(self, session: Session, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        key = self._key(session.session_id, tool, args)
        prior = self.conn.execute(f"SELECT result FROM {self.s}.idempotency_keys WHERE key = %s", (key,)).fetchone()
        if prior is not None:
            session.stats["duplicates"] += 1
            self._audit(session, tool, "write", args, "duplicate", "same write already executed in this session")
            return {"ok": True, "duplicate": True, "note": "This exact change was already applied; nothing was "
                                                           "done again.", "result": prior[0]}
        amount = self.amount_at_stake(tool, args)
        trace.get_current_span().set_attribute("order_ops.amount_usd", float(amount))
        if amount > Decimal(str(session.approval_threshold)) and not self.enforce:
            self._shadow(session, tool, "write", args, "approval")
        elif amount > Decimal(str(session.approval_threshold)):
            session.stats["approvals"] += 1
            if not session.auto_approve:
                session.confirmation = False
                session.confirmed = None
                approval_id = self._request_approval(session, tool, args, amount)
                self._audit(session, tool, "write", args, "pending_approval", f"{approval_id} ${amount}")
                session.note_done(tool, args, pending_approval=True)
                return {"ok": True, "pending_approval": True, "approval_id": approval_id,
                        "note": f"${amount} is above the ${session.approval_threshold:.0f} limit, so a human "
                                "supervisor must approve it. Tell the customer it is submitted and pending review."}
        result = self._execute_once(key, tool, args)
        session.confirmation = False  # one "yes" pays for one successful change
        session.confirmed = None
        session.stats["writes"] += 1
        session.note_done(tool, args)
        self._audit(session, tool, "write", args, "ok", f"${amount}")
        return {"ok": True, "result": result}

    def _execute_once(self, key: str, tool: str, args: dict[str, Any]) -> Any:
        with self.conn.transaction():  # the write and its idempotency key commit together
            result = call(self.store, tool, args)
            self.conn.execute(f"INSERT INTO {self.s}.idempotency_keys (key, tool, args, result) VALUES (%s,%s,%s,%s)",
                              (key, tool, Jsonb(args), Jsonb(result)))
        return result

    def _request_approval(self, session: Session, tool: str, args: dict[str, Any], amount: Decimal) -> str:
        approval_id = "apr_" + uuid.uuid4().hex[:10]
        summary = f"{tool} on {args.get('order_id', args.get('user_id'))}: ${amount}"
        self.conn.execute(
            f"INSERT INTO {self.s}.approvals (approval_id, session_id, user_id, order_id, tool, args, amount, summary) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (approval_id, session.session_id, session.user_id, args.get("order_id"), tool, Jsonb(args), amount,
             summary))
        return approval_id

    # ---------- audit ----------
    @staticmethod
    def _key(session_id: str, tool: str, args: dict[str, Any]) -> str:
        return hashlib.sha256(f"{session_id}|{tool}|{json.dumps(args, sort_keys=True)}".encode()).hexdigest()

    def _audit(self, session: Session, tool: str, tier: str, args: dict, outcome: str, detail: str = "") -> None:
        self._audit_raw(session.session_id, tool, tier, args, outcome, detail)

    def _audit_raw(self, session_id: str, tool: str, tier: str, args: dict, outcome: str, detail: str) -> None:
        self.conn.execute(
            f"INSERT INTO {self.s}.audit_log (session_id, tool, tier, args, outcome, detail) "
            f"VALUES (%s,%s,%s,%s,%s,%s)",
            (session_id, tool, tier, Jsonb(args), outcome, detail))
