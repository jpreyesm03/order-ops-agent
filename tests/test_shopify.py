"""Shopify backend without network: a fake Admin API client, a real Postgres mirror."""
from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

import pytest

from order_ops import db
from order_ops.gate import Gate, Session
from order_ops.seed_shopify import pick_slice, shopify_options
from order_ops.shopify import MIRROR_SQL, ShopifyError, ShopifyStore, apply_webhook, put_ids, verify_webhook


class FakeShop:
    """Records mutations; `fail` makes the next mutation fail, `cancelled` controls what Shopify reports back."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail: str | None = None
        self.cancelled = True

    def mutate(self, query, variables, field):
        self.calls.append(field)
        if self.fail:
            raise ShopifyError(self.fail)
        return {"job": None, "order": {"id": variables.get("id")}, "return": {"id": "r", "status": "OPEN"}}

    def gql(self, query, variables=None):
        return {"order": {"cancelledAt": "2026-11-01T00:00:00Z" if self.cancelled else None}}


@pytest.fixture
def mirror(monkeypatch):
    monkeypatch.setattr("order_ops.shopify.time.sleep", lambda s: None)
    conn = db.connect()
    data = pick_slice(db.load_tau2_db(), 2)
    schema = f"t_{uuid.uuid4().hex[:8]}"
    db.load_store(conn, schema, data)
    conn.execute(MIRROR_SQL.format(s=schema))
    rows = [("order", oid, f"gid://shopify/Order/{i}") for i, oid in enumerate(data["orders"])]
    rows += [("customer", u, f"gid://shopify/Customer/{i}") for i, u in enumerate(data["users"])]
    rows += [("customer_address", u, f"gid://shopify/MailingAddress/{i}") for i, u in enumerate(data["users"])]
    rows += [("fulfillment_line_item", f"{oid}|{p}", f"gid://shopify/FulfillmentLineItem/{i}{p}")
             for i, (oid, o) in enumerate(data["orders"].items()) for p in range(len(o["items"]))]
    put_ids(conn, schema, rows)
    pending = next(oid for oid, o in data["orders"].items() if o["status"] == "pending")
    yield conn, schema, data, pending
    db.drop_schema(conn, schema)
    conn.close()


def test_slice_has_every_demo_flow():
    data = pick_slice(db.load_tau2_db(), 4)
    assert len(data["users"]) == 4
    for u in data["users"].values():
        statuses = {data["orders"][o]["status"] for o in u["orders"]}
        assert {"pending", "delivered"} <= statuses
    used = {it["product_id"] for o in data["orders"].values() for it in o["items"]}
    assert used == set(data["products"])


def test_shopify_options_fit_the_three_option_limit():
    opts = shopify_options({"color": "blue", "size": "M", "material": "cotton", "style": "crew neck"})
    assert opts == [("color", "blue"), ("size", "M"), ("material and style", "cotton, crew neck")]


def test_write_goes_to_shopify_then_mirror(mirror):
    conn, schema, _, oid = mirror
    shop = FakeShop()
    out = ShopifyStore(conn, schema, shop).cancel_pending_order(oid, "no longer needed")
    assert shop.calls == ["orderCancel"]
    assert out["status"] == "cancelled" and db.get_order(conn, schema, oid)["status"] == "cancelled"


def test_shopify_rejection_rolls_back_the_mirror(mirror):
    conn, schema, _, oid = mirror
    shop = FakeShop()
    shop.fail = "Order is already fulfilled"
    with pytest.raises(ShopifyError):
        ShopifyStore(conn, schema, shop).cancel_pending_order(oid, "no longer needed")
    assert db.get_order(conn, schema, oid)["status"] == "pending"


def test_cancel_that_shopify_silently_dropped_rolls_back(mirror):
    """orderCancel is a background job; if the order isn't cancelled afterwards, the mirror must not say it is."""
    conn, schema, _, oid = mirror
    shop = FakeShop()
    shop.cancelled = False
    with pytest.raises(ShopifyError):
        ShopifyStore(conn, schema, shop).cancel_pending_order(oid, "no longer needed")
    assert db.get_order(conn, schema, oid)["status"] == "pending"


def test_local_rules_run_before_shopify(mirror):
    """A change the policy forbids never reaches Shopify."""
    conn, schema, data, _ = mirror
    delivered = next(oid for oid, o in data["orders"].items() if o["status"] == "delivered")
    shop = FakeShop()
    with pytest.raises(Exception, match="Non-pending"):
        ShopifyStore(conn, schema, shop).cancel_pending_order(delivered, "no longer needed")
    assert shop.calls == []


def test_gate_uses_the_shopify_store(mirror):
    conn, schema, data, oid = mirror
    shop = FakeShop()
    gate = Gate(conn, schema, ShopifyStore(conn, schema, shop))
    s = Session("shop-1", approval_threshold=1e9)
    gate.run(s, "find_user_id_by_email", {"email": data["users"][data["orders"][oid]["user_id"]]["email"]})
    assert gate.run(s, "cancel_pending_order", {"order_id": oid, "reason": "no longer needed"})[
        "needs_confirmation"]
    assert shop.calls == []
    s.on_customer_message("yes, cancel it")
    assert gate.run(s, "cancel_pending_order", {"order_id": oid, "reason": "no longer needed"})["ok"]
    assert shop.calls == ["orderCancel"]


def test_webhook_signature():
    body = b'{"id": 1}'
    good = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
    assert verify_webhook(body, good, "secret")
    assert not verify_webhook(body, good, "other-secret")
    assert not verify_webhook(b'{"id": 2}', good, "secret")
    assert not verify_webhook(body, "", "secret")


def test_webhook_applies_external_cancel_once(mirror):
    conn, schema, _, oid = mirror
    gid = conn.execute(f"SELECT gid FROM {schema}.shopify_ids WHERE kind='order' AND local_id=%s", (oid,)).fetchone()[0]
    payload = {"admin_graphql_api_id": gid}
    assert apply_webhook(conn, schema, "w1", "orders/cancelled", payload) == "mirror: cancelled"
    assert db.get_order(conn, schema, oid)["status"] == "cancelled"
    assert apply_webhook(conn, schema, "w1", "orders/cancelled", payload) == "duplicate"
    assert apply_webhook(conn, schema, "w2", "orders/cancelled", payload) == "in sync"
