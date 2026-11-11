"""Shared fixtures."""
from __future__ import annotations

import uuid

import pytest

from order_ops import db


@pytest.fixture
def env():
    conn = db.connect()
    have = conn.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name='base'").fetchone()
    if not have:
        db.load_store(conn, "base", db.load_tau2_db())
    schema = f"t_{uuid.uuid4().hex[:8]}"
    db.clone_schema(conn, "base", schema)
    oid, uid = conn.execute(f"""SELECT o.order_id, o.user_id FROM {schema}.orders o JOIN {schema}.order_payments p
                                USING (order_id) WHERE o.status='pending' AND p.amount < 200
                                ORDER BY o.order_id LIMIT 1""").fetchone()
    email = db.get_user(conn, schema, uid)["email"]
    yield conn, schema, oid, email
    db.drop_schema(conn, schema)
    conn.close()
