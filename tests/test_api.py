"""API tests that don't call the LLM: sessions, approval inbox, decisions, views, admin auth."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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
    s.on_customer_message("yes, cancel it")
    out = gate.run(s, "cancel_pending_order", {"order_id": oid, "reason": "no longer needed"})
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


def test_message_validation(client):
    sid = client.post("/api/sessions").json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": ""}).status_code == 422
