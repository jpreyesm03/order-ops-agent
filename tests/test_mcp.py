"""MCP server end to end over stdio, the way Claude Desktop talks to it."""
from __future__ import annotations

import json
import os
import sys
import uuid

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from order_ops import db


@pytest.fixture
def schema():
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name='base'").fetchone():
        db.load_store(conn, "base", db.load_tau2_db())
    name = f"t_{uuid.uuid4().hex[:8]}"
    db.clone_schema(conn, "base", name)
    oid, uid = conn.execute(f"""SELECT o.order_id, o.user_id FROM {name}.orders o JOIN {name}.order_payments p
                                USING (order_id) WHERE o.status='pending' AND p.amount < 200
                                ORDER BY o.order_id LIMIT 1""").fetchone()
    email = db.get_user(conn, name, uid)["email"]
    yield name, oid, email
    db.drop_schema(conn, name)
    conn.close()


def _text(result) -> dict:
    return json.loads(result.content[0].text)


def test_mcp_server_enforces_the_gate(schema):
    name, oid, email = schema

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "order_ops.mcp_server"],
                                       env={**os.environ, "MCP_SCHEMA": name})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert tools["cancel_pending_order"].annotations.destructive_hint
            assert tools["get_order_details"].annotations.read_only_hint

            out = _text(await session.call_tool("get_order_details", {"order_id": oid}))
            assert out["blocked_by_policy"] == "identity"
            await session.call_tool("find_user_id_by_email", {"email": email})
            out = _text(await session.call_tool("cancel_pending_order", {
                "order_id": oid, "reason": "no longer needed", "customer_confirmation": "what does that mean?"}))
            assert out["blocked_by_policy"] == "confirmation"
            out = _text(await session.call_tool("cancel_pending_order", {
                "order_id": oid, "reason": "no longer needed", "customer_confirmation": "Yes, cancel it."}))
            assert out["ok"] and out["result"]["status"] == "cancelled"

    anyio.run(run)
