"""HTTP API for the live demo: customer chat, supervisor approval inbox, order/customer views, audit log.

    uv run uvicorn order_ops.api:app --port 8055 --reload
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

import anthropic
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import db
from .agent import Agent
from .gate import Gate, Session
from .shopify import MIRROR_SQL, ShopifyClient, ShopifyStore, apply_webhook, verify_webhook
from .store import ToolError

# tau2: the benchmark store in Postgres. shopify: a real Shopify dev store, mirrored in Postgres.
BACKEND = os.getenv("STORE_BACKEND", "tau2")
LIVE = "shop" if BACKEND == "shopify" else "live"
GREETING = "Hi! I'm the store's support assistant. I can help with order status, cancellations, address or " \
           "payment changes, returns and exchanges. What can I do for you?"
THRESHOLD = float(os.getenv("APPROVAL_THRESHOLD_USD", "250"))
MAX_TURNS_PER_SESSION = int(os.getenv("MAX_TURNS_PER_SESSION", "40"))
TURNS_PER_IP_PER_HOUR = int(os.getenv("TURNS_PER_IP_PER_HOUR", "60"))
TURNS_PER_DAY = int(os.getenv("TURNS_PER_DAY", "1500"))

_conn_lock = threading.Lock()
_client: anthropic.Anthropic | None = None
_shopify: ShopifyClient | None = None
_reseed_lock = threading.Lock()


def make_gate(conn) -> Gate:
    global _shopify
    if BACKEND != "shopify":
        return Gate(conn, LIVE)
    if _shopify is None:
        _shopify = ShopifyClient()
    return Gate(conn, LIVE, ShopifyStore(conn, LIVE, _shopify))


def ensure_live(conn) -> None:
    """Create the base store and the live copy on first boot."""
    have = {r[0] for r in conn.execute("SELECT schema_name FROM information_schema.schemata")}
    if BACKEND == "shopify":
        if LIVE not in have:
            raise RuntimeError("No Shopify mirror yet: run `python -m order_ops.seed_shopify` first.")
        conn.execute(MIRROR_SQL.format(s=LIVE))
        return
    if "base" not in have:
        db.load_store(conn, "base", db.load_tau2_db())
    if LIVE not in have:
        db.clone_schema(conn, "base", LIVE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    conn = db.connect()
    ensure_live(conn)
    conn.close()
    _client = anthropic.Anthropic(max_retries=4)
    yield


app = FastAPI(title="Order Ops Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3005").split(",") if o.strip()],
    allow_methods=["*"], allow_headers=["*"],
)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------- rate limits (protect the API key on a public demo) ----------
_hits: dict[str, list[float]] = defaultdict(list)
_day = {"date": time.strftime("%Y-%m-%d"), "count": 0}
_rl_lock = threading.Lock()


def rate_limit(request: Request) -> None:
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    now = time.time()
    with _rl_lock:
        today = time.strftime("%Y-%m-%d")
        if _day["date"] != today:
            _day.update(date=today, count=0)
        if _day["count"] >= TURNS_PER_DAY:
            raise HTTPException(429, "The demo reached its daily limit. Please come back tomorrow.")
        recent = [t for t in _hits[ip] if now - t < 3600]
        if len(recent) >= TURNS_PER_IP_PER_HOUR:
            raise HTTPException(429, "Too many messages from your network this hour. Please try again later.")
        recent.append(now)
        _hits[ip] = recent
        _day["count"] += 1


# ---------- models ----------
class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class DecisionIn(BaseModel):
    approve: bool
    reviewer: str = Field(default="supervisor", max_length=60)
    note: str = Field(default="", max_length=500)


# ---------- sessions / chat ----------
def _load_session(conn, session_id: str) -> tuple[Session, list[dict], dict]:
    row = conn.execute(f"SELECT state, messages FROM {LIVE}.sessions WHERE session_id = %s", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Session not found")
    state, messages = row
    extra = state.pop("_extra", {"transcript": [], "notified": []})
    return Session.from_json(state), messages, extra


def _save_session(conn, session: Session, messages: list[dict], extra: dict) -> None:
    state = session.to_json() | {"_extra": extra}
    conn.execute(f"UPDATE {LIVE}.sessions SET state = %s, messages = %s, updated_at = now() WHERE session_id = %s",
                 (Jsonb(state), Jsonb(messages), session.session_id))


def _approvals_for(conn, session_id: str) -> list[dict]:
    rows = conn.execute(
        f"SELECT approval_id, tool, order_id, amount, summary, status, decided_by, decision_note, created_at, "
        f"decided_at FROM {LIVE}.approvals WHERE session_id = %s ORDER BY created_at", (session_id,)).fetchall()
    return [_approval_dict(r) for r in rows]


def _approval_dict(r) -> dict:
    keys = ["approval_id", "tool", "order_id", "amount", "summary", "status", "decided_by", "decision_note",
            "created_at", "decided_at"]
    d = dict(zip(keys, r, strict=True))
    d["amount"] = db.money(d["amount"])
    return d


@app.get("/healthz")
def healthz(conn=Depends(get_conn)) -> dict:
    conn.execute("SELECT 1")
    return {"ok": True, "backend": BACKEND}


@app.post("/api/sessions")
def create_session(conn=Depends(get_conn)) -> dict:
    sid = "ses_" + uuid.uuid4().hex[:12]
    session = Session(session_id=sid, approval_threshold=THRESHOLD)
    extra = {"transcript": [{"role": "assistant", "text": GREETING, "ts": time.time()}], "notified": []}
    conn.execute(f"INSERT INTO {LIVE}.sessions (session_id, state, messages) VALUES (%s, %s, %s)",
                 (sid, Jsonb(session.to_json() | {"_extra": extra}), Jsonb([])))
    return {"session_id": sid, "greeting": GREETING, "approval_threshold": THRESHOLD}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, conn=Depends(get_conn)) -> dict:
    session, _, extra = _load_session(conn, session_id)
    return {"session_id": session_id, "user_id": session.user_id, "transcript": extra["transcript"],
            "approvals": _approvals_for(conn, session_id), "gate": session.stats}


@app.post("/api/sessions/{session_id}/messages", dependencies=[Depends(rate_limit)])
def post_message(session_id: str, body: MessageIn, conn=Depends(get_conn)) -> dict:
    session, messages, extra = _load_session(conn, session_id)
    if sum(1 for t in extra["transcript"] if t["role"] == "user") >= MAX_TURNS_PER_SESSION:
        raise HTTPException(429, "This conversation is too long for the demo. Start a new one.")
    # tell the agent about supervisor decisions it hasn't seen yet
    notices = []
    for a in _approvals_for(conn, session_id):
        if a["status"] != "pending" and a["approval_id"] not in extra["notified"]:
            notices.append(f"Supervisor update: request {a['approval_id']} ({a['summary']}) was {a['status']}"
                           + (f". Note: {a['decision_note']}" if a["decision_note"] else "") + ".")
            extra["notified"].append(a["approval_id"])
    agent = Agent(make_gate(conn), client=_client)
    started = time.perf_counter()
    turn = agent.respond(session, messages, body.text, notices=notices)
    latency_ms = round((time.perf_counter() - started) * 1000)
    now = time.time()
    events = [{"tool": c["name"], "args": c["args"], "ok": c["result"].get("ok", False),
               "blocked": c["result"].get("blocked_by_policy"), "pending_approval": c["result"].get(
                   "pending_approval", False), "error": c["result"].get("error")} for c in turn.tool_calls]
    extra["transcript"] += [{"role": "user", "text": body.text, "ts": now},
                            {"role": "assistant", "text": turn.reply, "ts": time.time(), "events": events,
                             "latency_ms": latency_ms, "cost_usd": round(agent.usage.cost(agent.model), 5)}]
    _save_session(conn, session, messages, extra)
    return {"reply": turn.reply, "events": events, "transferred": turn.transferred, "user_id": session.user_id,
            "approvals": _approvals_for(conn, session_id), "latency_ms": latency_ms,
            "cost_usd": round(agent.usage.cost(agent.model), 5)}


# ---------- supervisor console ----------
@app.get("/api/approvals")
def list_approvals(status: str = "pending", conn=Depends(get_conn)) -> list[dict]:
    where, params = ("", ()) if status == "all" else ("WHERE status = %s", (status,))
    rows = conn.execute(
        f"SELECT approval_id, tool, order_id, amount, summary, status, decided_by, decision_note, created_at, "
        f"decided_at FROM {LIVE}.approvals {where} ORDER BY created_at DESC LIMIT 200", params).fetchall()
    return [_approval_dict(r) for r in rows]


@app.post("/api/approvals/{approval_id}/decision")
def decide(approval_id: str, body: DecisionIn, conn=Depends(get_conn)) -> dict:
    try:
        return make_gate(conn).decide(approval_id, body.approve, body.reviewer, body.note)
    except ToolError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/orders/{order_id}")
def order(order_id: str, conn=Depends(get_conn)) -> dict:
    o = db.get_order(conn, LIVE, order_id if order_id.startswith("#") else "#" + order_id)
    if o is None:
        raise HTTPException(404, "Order not found")
    return o


@app.get("/api/customers/{user_id}")
def customer(user_id: str, conn=Depends(get_conn)) -> dict[str, Any]:
    u = db.get_user(conn, LIVE, user_id)
    if u is None:
        raise HTTPException(404, "Customer not found")
    u["order_details"] = [db.get_order(conn, LIVE, oid) for oid in u["orders"]]
    return u


@app.get("/api/audit")
def audit(session_id: str | None = None, limit: int = 100, conn=Depends(get_conn)) -> list[dict]:
    where, params = ("WHERE session_id = %s", (session_id, min(limit, 500))) if session_id else ("", (min(limit, 500),))
    rows = conn.execute(f"SELECT id, ts, session_id, tool, tier, args, outcome, detail FROM {LIVE}.audit_log "
                        f"{where} ORDER BY id DESC LIMIT %s", params).fetchall()
    keys = ["id", "ts", "session_id", "tool", "tier", "args", "outcome", "detail"]
    return [dict(zip(keys, r, strict=True)) for r in rows]


@app.get("/api/stats")
def stats(conn=Depends(get_conn)) -> dict:
    outcomes = dict(conn.execute(f"SELECT outcome, count(*) FROM {LIVE}.audit_log GROUP BY outcome").fetchall())
    blocked = dict(conn.execute(f"SELECT split_part(detail, ':', 1), count(*) FROM {LIVE}.audit_log "
                                f"WHERE outcome = 'blocked' GROUP BY 1").fetchall())
    sessions = conn.execute(f"SELECT count(*) FROM {LIVE}.sessions").fetchone()[0]
    return {"sessions": sessions, "tool_outcomes": outcomes, "blocked_by_rule": blocked}


@app.get("/api/demo/customers")
def demo_customers(conn=Depends(get_conn)) -> list[dict]:
    """A few customers a visitor can role-play, each with a pending and a delivered order."""
    rows = conn.execute(f"""
        SELECT u.user_id, u.first_name, u.last_name, u.email, u.address->>'zip',
               min(o.order_id) FILTER (WHERE o.status = 'pending'),
               min(o.order_id) FILTER (WHERE o.status = 'delivered')
        FROM {LIVE}.users u JOIN {LIVE}.orders o USING (user_id)
        GROUP BY u.user_id HAVING count(*) FILTER (WHERE o.status = 'pending') > 0
                              AND count(*) FILTER (WHERE o.status = 'delivered') > 0
        ORDER BY md5(u.user_id) LIMIT 6""").fetchall()
    keys = ["user_id", "first_name", "last_name", "email", "zip", "pending_order", "delivered_order"]
    return [dict(zip(keys, r, strict=True)) for r in rows]


@app.post("/api/admin/reset")
def reset(x_admin_token: str = Header(default=""), conn=Depends(get_conn)) -> dict:
    token = os.getenv("ADMIN_TOKEN")
    if not token or x_admin_token != token:
        raise HTTPException(403, "Forbidden")
    if BACKEND == "shopify":  # delete the seeded Shopify orders and seed them again (takes ~1 min)
        if not _reseed_lock.acquire(blocking=False):
            return {"ok": True, "status": "already reseeding"}
        threading.Thread(target=_reseed, daemon=True).start()
        return {"ok": True, "status": "reseeding"}
    with _conn_lock:
        db.clone_schema(conn, "base", LIVE)
    return {"ok": True}


def _reseed() -> None:
    try:
        subprocess.run([sys.executable, "-m", "order_ops.seed_shopify", "--reset", "--schema", LIVE,
                        "--users", os.getenv("SHOPIFY_SEED_USERS", "8")], check=False, timeout=900)
    finally:
        _reseed_lock.release()


@app.post("/webhooks/shopify")
async def shopify_webhook(request: Request) -> dict:
    """Shopify → mirror. Only signed requests are accepted (HMAC-SHA256 of the raw body with the app secret)."""
    body = await request.body()
    if BACKEND != "shopify" or not verify_webhook(body, request.headers.get("x-shopify-hmac-sha256", ""),
                                                 os.getenv("SHOPIFY_CLIENT_SECRET", "")):
        raise HTTPException(401, "Invalid signature")
    webhook_id = request.headers.get("x-shopify-webhook-id") or request.headers.get("x-shopify-event-id", "")
    topic = request.headers.get("x-shopify-topic", "")

    def apply() -> str:
        conn = db.connect()
        try:
            return apply_webhook(conn, LIVE, webhook_id, topic, json.loads(body))
        finally:
            conn.close()

    return {"ok": True, "action": await run_in_threadpool(apply)}
