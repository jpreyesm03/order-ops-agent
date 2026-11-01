"""MCP server: the store's tools for any MCP client (Claude Desktop, Claude Code, ...), behind the same policy gate.

One server process = one conversation = one gate Session. Writes take `customer_confirmation`: the customer's own
words confirming the change. The gate checks it like a chat message, and MCP clients additionally ask their user
to approve each tool call. Money movements above the limit still go to the supervisor queue.

    uv run python -m order_ops.mcp_server          # stdio
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import db
from .gate import Gate, Session

SCHEMA = os.getenv("MCP_SCHEMA", "live")
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

server = MCPServer(
    "order-ops",
    instructions="Retail store operations under a written policy. Authenticate the customer first "
                 "(find_user_id_by_email or find_user_id_by_name_zip). Before any change, show the details to "
                 "the customer and pass their exact confirming words as customer_confirmation.",
)

_conn = None
_session = Session(session_id="mcp_" + uuid.uuid4().hex[:10],
                   approval_threshold=float(os.getenv("APPROVAL_THRESHOLD_USD", "250")))


def _gate() -> Gate:
    global _conn
    if _conn is None or _conn.closed:
        _conn = db.connect()
    return Gate(_conn, SCHEMA)


def _read(tool: str, **args: Any) -> str:
    return json.dumps(_gate().run(_session, tool, args), default=str)


def _write(tool: str, customer_confirmation: str, **args: Any) -> str:
    _session.on_customer_message(customer_confirmation)
    return json.dumps(_gate().run(_session, tool, args), default=str)


# ---------- read ----------
@server.tool(annotations=READ)
def find_user_id_by_email(email: str) -> str:
    """Find a customer's user id by email. This authenticates the customer for the rest of the session."""
    return _read("find_user_id_by_email", email=email)


@server.tool(annotations=READ)
def find_user_id_by_name_zip(first_name: str, last_name: str, zip: str) -> str:
    """Find a customer's user id by first name, last name and zip code (authenticates the customer)."""
    return _read("find_user_id_by_name_zip", first_name=first_name, last_name=last_name, zip=zip)


@server.tool(annotations=READ)
def get_user_details(user_id: str) -> str:
    """Profile, payment methods and order ids of the authenticated customer."""
    return _read("get_user_details", user_id=user_id)


@server.tool(annotations=READ)
def get_order_details(order_id: str) -> str:
    """Status, items, payments and fulfillment of one of the authenticated customer's orders (e.g. '#W0000000')."""
    return _read("get_order_details", order_id=order_id)


@server.tool(annotations=READ)
def get_product_details(product_id: str) -> str:
    """All variants of a product (item ids, options, availability, price). Product id is not an item id."""
    return _read("get_product_details", product_id=product_id)


@server.tool(annotations=READ)
def list_all_product_types() -> str:
    """Names and product ids of all 50 product types."""
    return _read("list_all_product_types")


# ---------- write (gate: identity, ownership, confirmation, approval limit, idempotency) ----------
@server.tool(annotations=WRITE)
def cancel_pending_order(order_id: str, reason: Literal["no longer needed", "ordered by mistake"],
                         customer_confirmation: str) -> str:
    """Cancel a pending order and refund it. customer_confirmation = the customer's exact words saying yes."""
    return _write("cancel_pending_order", customer_confirmation, order_id=order_id, reason=reason)


@server.tool(annotations=WRITE)
def modify_pending_order_address(order_id: str, address1: str, address2: str, city: str, state: str, country: str,
                                 zip: str, customer_confirmation: str) -> str:
    """Change the shipping address of a pending order."""
    return _write("modify_pending_order_address", customer_confirmation, order_id=order_id, address1=address1,
                  address2=address2, city=city, state=state, country=country, zip=zip)


@server.tool(annotations=WRITE)
def return_delivered_order_items(order_id: str, item_ids: list[str], payment_method_id: str,
                                 customer_confirmation: str) -> str:
    """Return items of a delivered order; refund to the original payment method or a gift card."""
    return _write("return_delivered_order_items", customer_confirmation, order_id=order_id, item_ids=item_ids,
                  payment_method_id=payment_method_id)


@server.tool(annotations=WRITE)
def exchange_delivered_order_items(order_id: str, item_ids: list[str], new_item_ids: list[str],
                                   payment_method_id: str, customer_confirmation: str) -> str:
    """Exchange delivered items for other available options of the same product (once per order)."""
    return _write("exchange_delivered_order_items", customer_confirmation, order_id=order_id, item_ids=item_ids,
                  new_item_ids=new_item_ids, payment_method_id=payment_method_id)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
