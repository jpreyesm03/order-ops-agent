"""API tests that don't call the LLM: sessions, approval inbox, decisions, views, admin auth."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from helpers import confirmed_run
from order_ops import api, db
from order_ops.gate import Gate, Session


@pytest.fixture(scope="module")
def client():
    with TestClient(api.app) as c:
        yield c


def test_health_and_session(client):
    assert client.get("/healthz").json() == {"ok": True, "backend": "tau2"}
    s = client.post("/api/sessions").json()
    assert s["session_id"].startswith("ses_") and s["greeting"]
    got = client.get(f"/api/sessions/{s['session_id']}").json()
    assert got["user_id"] is None and got["transcript"][0]["role"] == "assistant"
    assert client.get("/api/sessions/ses_nope").status_code == 404


def test_demo_customers_have_orders(client):
    rows = client.get("/api/demo/customers").json()
    assert rows and all(r["pending_order"] and r["delivered_order"] for r in rows)


def test_order_and_customer_views(client):
    assert client.get("/api/orders/W2611340").json()["order_id"] == "#W2611340"
    assert client.get("/api/orders/%23nope").status_code == 404
    assert client.get("/api/customers/noah_brown_6181").json()["order_details"][0]["order_id"] == "#W7678072"


def test_approval_inbox_and_decision(client):
    """A big cancellation queued by the gate shows up in the inbox; approving it executes it exactly once."""
    conn = db.connect()
    oid, uid = conn.execute(f"""SELECT o.order_id, o.user_id FROM {api.LIVE}.orders o
                                JOIN {api.LIVE}.order_payments p USING (order_id)
                                WHERE o.status = 'pending' AND p.amount > 300 ORDER BY random() LIMIT 1""").fetchone()
    gate = Gate(conn, api.LIVE)
    s = Session("ses_test_" + oid.strip("#"), approval_threshold=250)
    gate.run(s, "find_user_id_by_email", {"email": db.get_user(conn, api.LIVE, uid)["email"]})
    out = confirmed_run(gate, s, "cancel_pending_order", {"order_id": oid, "reason": "no longer needed"},
                        "yes, cancel it")
    conn.close()
    aid = out["approval_id"]
    assert aid in [a["approval_id"] for a in client.get("/api/approvals").json()]
    r = client.post(f"/api/approvals/{aid}/decision", json={"approve": True, "reviewer": "tester"})
    assert r.json()["status"] == "approved"
    assert client.get(f"/api/orders/{oid.strip('#')}").json()["status"] == "cancelled"
    assert client.post(f"/api/approvals/{aid}/decision", json={"approve": True}).status_code == 409


def test_audit_and_stats(client):
    assert isinstance(client.get("/api/audit?limit=5").json(), list)
    assert "blocked_by_rule" in client.get("/api/stats").json()


def test_reset_needs_token(client):
    assert client.post("/api/admin/reset").status_code == 403
    assert client.post("/api/admin/reset", headers={"x-admin-token": "wrong"}).status_code == 403


def test_events_need_token(client, monkeypatch):
    """/events is reachable through the public URL too, so every task must carry the admin token."""
    assert client.post("/events", json={"task": "reseed"}).status_code == 403
    monkeypatch.setenv("ADMIN_TOKEN", "t0ken")
    assert client.post("/events", json={"task": "reseed", "token": "wrong"}).status_code == 403
    assert client.post("/events", json={"task": "reseed", "token": "t0ken"}).status_code == 400  # tau2: no reseed


def test_rate_limit_is_shared_in_postgres(client, monkeypatch):
    """The per-IP limit is counted in Postgres (all instances see it); a rejected message doesn't count."""
    monkeypatch.setattr(api, "TURNS_PER_IP_PER_HOUR", 2)
    addr = f"203.0.113.{uuid.uuid4().int % 250}"
    ctx = {"x-amzn-request-context": json.dumps({"http": {"sourceIp": addr}})}
    sid = client.post("/api/sessions").json()["session_id"]

    def post(spoof: str) -> int:  # a client-chosen X-Forwarded-For must not open a fresh window
        headers = ctx | {"x-forwarded-for": spoof}
        return client.post(f"/api/sessions/{sid}/messages", json={"text": ""}, headers=headers).status_code

    assert [post("1.1.1.1"), post("2.2.2.2"), post("3.3.3.3"), post("4.4.4.4")] == [422, 422, 429, 429]
    conn = db.connect()
    bucket = f"ip:{addr}:%"
    assert conn.execute("SELECT hits FROM public.rate_limits WHERE bucket LIKE %s", (bucket,)).fetchone()[0] == 2
    conn.execute("DELETE FROM public.rate_limits WHERE bucket LIKE %s", (bucket,))
    conn.close()


def test_message_validation(client):
    sid = client.post("/api/sessions").json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": ""}).status_code == 422
