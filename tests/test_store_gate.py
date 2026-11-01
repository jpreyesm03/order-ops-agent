"""Tests on a real Postgres (docker compose). Each test gets its own schema cloned from `base`."""
from __future__ import annotations

import json
import uuid

import pytest

from order_ops import db
from order_ops.evaluate import gold_state, load_tasks
from order_ops.gate import Gate, Session, is_confirmation
from order_ops.store import Store, ToolError


@pytest.fixture(scope="session")
def conn():
    c = db.connect()
    exists = c.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'base'").fetchone()
    if not exists:
        db.load_store(c, "base", db.load_tau2_db())
    yield c
    c.close()


@pytest.fixture
def schema(conn):
    name = f"t_{uuid.uuid4().hex[:8]}"
    db.clone_schema(conn, "base", name)
    yield name
    db.drop_schema(conn, name)


def _first(conn, schema, status):
    return conn.execute(f"SELECT order_id, user_id FROM {schema}.orders WHERE status = %s ORDER BY order_id LIMIT 1",
                        (status,)).fetchone()


# ---------- data layer ----------
def test_round_trip_matches_source_json(conn):
    """Loading db.json into Postgres and exporting it back gives the exact same store."""
    assert db.canonical(db.export_state(conn, "base")) == db.canonical(db.load_tau2_db())


def test_bad_schema_name_rejected():
    with pytest.raises(ValueError):
        db.ident("x; DROP TABLE users")


def test_gold_replay_changes_store(conn):
    task = next(t for t in load_tasks("all") if t["id"] == "0")
    assert gold_state(conn, task) != db.canonical(db.export_state(conn, "base"))


# ---------- store data rules ----------
def test_cancel_refunds_and_rejects_bad_reason(conn, schema):
    store = Store(conn, schema)
    oid, _ = _first(conn, schema, "pending")
    with pytest.raises(ToolError, match="Invalid reason"):
        store.cancel_pending_order(oid, "too expensive")
    o = store.cancel_pending_order(oid, "no longer needed")
    assert o["status"] == "cancelled"
    assert [p["transaction_type"] for p in o["payment_history"]] == ["payment", "refund"]
    with pytest.raises(ToolError, match="Non-pending"):
        store.cancel_pending_order(oid, "no longer needed")


def test_failed_write_leaves_no_trace(conn, schema):
    """A write that fails half-way rolls back completely (one transaction per tool call)."""
    store = Store(conn, schema)
    oid, _ = _first(conn, schema, "delivered")
    before = db.get_order(conn, schema, oid)
    with pytest.raises(ToolError):
        store.exchange_delivered_order_items(oid, [before["items"][0]["item_id"]], ["does-not-exist"], "nope")
    assert db.get_order(conn, schema, oid) == before


def test_calculate_is_safe():
    store = Store.__new__(Store)
    assert store.calculate("(2 + 3) * 4 / 2") == "10.0"
    with pytest.raises(ToolError):
        store.calculate("__import__('os')")


# ---------- policy gate ----------
def test_confirmation_detection():
    assert is_confirmation("Yes, please go ahead.")
    assert is_confirmation("yes, that's all, no other items")
    assert not is_confirmation("No, wait. Let me check first.")
    assert not is_confirmation("What is the refund amount?")


def test_writes_need_identity_then_confirmation(conn, schema):
    gate = Gate(conn, schema)
    oid, uid = _first(conn, schema, "pending")
    s = Session("s1")
    out = gate.run(s, "cancel_pending_order", {"order_id": oid, "reason": "no longer needed"})
    assert out["blocked_by_policy"] == "identity"
    email = db.get_user(conn, schema, uid)["email"]
    assert gate.run(s, "find_user_id_by_email", {"email": email})["result"] == uid
    out = gate.run(s, "cancel_pending_order", {"order_id": oid, "reason": "no longer needed"})
    assert out["blocked_by_policy"] == "confirmation"
    s.on_customer_message("Yes, cancel it.")
    assert gate.run(s, "cancel_pending_order", {"order_id": oid, "reason": "no longer needed"})["ok"]


def test_other_customers_orders_are_blocked(conn, schema):
    gate = Gate(conn, schema)
    oid, uid = _first(conn, schema, "pending")
    other = conn.execute(f"SELECT user_id, email FROM {schema}.users WHERE user_id <> %s LIMIT 1", (uid,)).fetchone()
    s = Session("s2")
    gate.run(s, "find_user_id_by_email", {"email": other[1]})
    assert gate.run(s, "get_order_details", {"order_id": oid})["blocked_by_policy"] == "ownership"
    me = db.get_user(conn, schema, uid)["email"]
    assert gate.run(s, "find_user_id_by_email", {"email": me})["blocked_by_policy"] == "one_customer"


def test_one_yes_pays_for_one_write(conn, schema):
    gate = Gate(conn, schema)
    rows = conn.execute(f"""SELECT o.order_id, o.user_id FROM {schema}.orders o
                            WHERE o.status = 'pending' AND o.user_id IN (
                              SELECT user_id FROM {schema}.orders WHERE status='pending'
                              GROUP BY user_id HAVING count(*) >= 2)
                            ORDER BY o.user_id, o.order_id LIMIT 2""").fetchall()
    (o1, uid), (o2, _) = rows
    s = Session("s3", approval_threshold=1e9)
    gate.run(s, "find_user_id_by_email", {"email": db.get_user(conn, schema, uid)["email"]})
    s.on_customer_message("yes")
    assert gate.run(s, "cancel_pending_order", {"order_id": o1, "reason": "no longer needed"})["ok"]
    assert gate.run(s, "cancel_pending_order", {"order_id": o2, "reason": "no longer needed"})[
        "blocked_by_policy"] == "confirmation"


def test_cannot_refund_twice(conn, schema):
    """A retried identical write is answered from the idempotency table, not executed again."""
    gate = Gate(conn, schema)
    oid, uid = _first(conn, schema, "pending")
    s = Session("s4", approval_threshold=1e9)
    gate.run(s, "find_user_id_by_email", {"email": db.get_user(conn, schema, uid)["email"]})
    args = {"order_id": oid, "reason": "ordered by mistake"}
    s.on_customer_message("yes please")
    first = gate.run(s, "cancel_pending_order", args)
    s.on_customer_message("yes")
    second = gate.run(s, "cancel_pending_order", args)
    assert first["ok"] and second.get("duplicate")
    refunds = conn.execute(f"SELECT count(*) FROM {schema}.order_payments WHERE order_id=%s AND "
                           f"transaction_type='refund'", (oid,)).fetchone()[0]
    assert refunds == 1


def test_big_refund_waits_for_human_then_executes(conn, schema):
    gate = Gate(conn, schema)
    oid, uid = conn.execute(f"""SELECT o.order_id, o.user_id FROM {schema}.orders o JOIN {schema}.order_payments p
                                USING (order_id) WHERE o.status='pending' AND p.amount > 300
                                ORDER BY o.order_id LIMIT 1""").fetchone()
    s = Session("s5", approval_threshold=250)
    gate.run(s, "find_user_id_by_email", {"email": db.get_user(conn, schema, uid)["email"]})
    s.on_customer_message("Yes, cancel it")
    out = gate.run(s, "cancel_pending_order", {"order_id": oid, "reason": "no longer needed"})
    assert out["pending_approval"]
    assert db.get_order(conn, schema, oid)["status"] == "pending"  # nothing happened yet
    decided = gate.decide(out["approval_id"], approve=True, reviewer="supervisor")
    assert decided["status"] == "approved"
    assert db.get_order(conn, schema, oid)["status"] == "cancelled"
    with pytest.raises(ToolError, match="already"):
        gate.decide(out["approval_id"], approve=True, reviewer="supervisor")


def test_audit_log_records_blocks(conn, schema):
    gate = Gate(conn, schema)
    gate.run(Session("s6"), "get_user_details", {"user_id": "noah_brown_6181"})
    row = conn.execute(f"SELECT outcome, detail FROM {schema}.audit_log WHERE session_id='s6'").fetchone()
    assert row[0] == "blocked" and row[1].startswith("identity")


def test_tool_results_are_json_serializable(conn, schema):
    gate = Gate(conn, schema)
    s = Session("s7")
    gate.run(s, "find_user_id_by_email", {"email": "noah.brown7922@example.com"})
    out = gate.run(s, "get_user_details", {"user_id": "noah_brown_6181"})
    json.dumps(out)
    assert out["result"]["orders"] == ["#W7678072"]
