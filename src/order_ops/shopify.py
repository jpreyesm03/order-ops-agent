"""Shopify backend: the store's writes go to a real Shopify store, reads come from a Postgres mirror.

    agent ─▶ gate ─▶ ShopifyStore.write
                       1. same business rules + change staged in the Postgres mirror (savepoint)
                       2. Shopify Admin GraphQL mutation (system of record)
                       3. commit the mirror only if Shopify accepted the change
    Shopify ─▶ signed webhook ─▶ apply_webhook ─▶ mirror (changes made by staff in the Shopify admin)

Reads stay on Postgres: the gate's ownership checks, the console and the audit log need fast SQL, and Shopify's
API is rate-limited. Local ids (τ² user ids, item ids, order ids) map to Shopify GIDs in `<schema>.shopify_ids`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from decimal import Decimal
from typing import Any

import httpx
import psycopg

from . import db
from .store import Store, ToolError

API_VERSION = "2026-10"

MIRROR_SQL = """
CREATE TABLE IF NOT EXISTS {s}.shopify_ids (
    kind text NOT NULL, local_id text NOT NULL, gid text NOT NULL, PRIMARY KEY (kind, local_id));
CREATE TABLE IF NOT EXISTS {s}.shopify_events (
    webhook_id text PRIMARY KEY, topic text NOT NULL, order_id text, action text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now());
"""


class ShopifyError(ToolError):
    """Shopify refused the change (the message is shown to the agent)."""


class ShopifyClient:
    """Admin GraphQL client. Access tokens come from the client-credentials grant and are renewed before expiry."""

    def __init__(self, shop: str | None = None, client_id: str | None = None, client_secret: str | None = None,
                 http: httpx.Client | None = None):
        self.shop = shop or os.environ["SHOPIFY_STORE"]
        self.client_id = client_id or os.environ["SHOPIFY_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["SHOPIFY_CLIENT_SECRET"]
        self.http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() > self._expires - 300:
                r = self.http.post(f"https://{self.shop}/admin/oauth/access_token", data={
                    "grant_type": "client_credentials", "client_id": self.client_id,
                    "client_secret": self.client_secret})
                r.raise_for_status()
                body = r.json()
                self._token, self._expires = body["access_token"], time.time() + int(body.get("expires_in", 3600))
            return self._token

    def gql(self, query: str, variables: dict | None = None) -> dict:
        url = f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json"
        refresh = False
        for attempt in range(5):
            try:
                r = self.http.post(url, json={"query": query, "variables": variables or {}},
                                   headers={"X-Shopify-Access-Token": self.token(force=refresh)})
            except httpx.HTTPError as e:
                if attempt == 4:
                    raise ToolError("The store system is not reachable right now; try again shortly.") from e
                time.sleep(1 + attempt)
                continue
            refresh = r.status_code == 401  # token revoked or expired early: get a new one and retry
            if refresh or r.status_code >= 500 or r.status_code == 429:
                time.sleep(0 if refresh else 1 + attempt)
                continue
            body = r.json()
            errors = body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                time.sleep(2 + attempt)
                continue
            if errors:
                raise ShopifyError("Store system error: " + "; ".join(e.get("message", "") for e in errors))
            return body["data"]
        raise ToolError("The store system is busy right now; try again shortly.")

    def mutate(self, query: str, variables: dict, field: str) -> dict:
        """Run a mutation and raise ShopifyError if it returned user errors."""
        payload = self.gql(query, variables)[field] or {}
        errors = [e for k, v in payload.items() if k.endswith("serErrors") and v for e in v]
        if errors:
            raise ShopifyError("Shopify rejected the change: " + "; ".join(e["message"] for e in errors))
        return payload


def address_input(a: dict, first_name: str = "", last_name: str = "") -> dict:
    return {"address1": a["address1"], "address2": a.get("address2", ""), "city": a["city"],
            "provinceCode": a["state"], "countryCode": "US" if a["country"] in ("USA", "US") else a["country"],
            "zip": a["zip"], "firstName": first_name, "lastName": last_name}


# ---------- id map ----------
def put_ids(conn: psycopg.Connection, schema: str, rows: list[tuple[str, str, str]]) -> None:
    s = db.ident(schema)
    with conn.cursor() as cur:
        cur.executemany(f"""INSERT INTO {s}.shopify_ids (kind, local_id, gid) VALUES (%s, %s, %s)
                            ON CONFLICT (kind, local_id) DO UPDATE SET gid = EXCLUDED.gid""", rows)


def get_id(conn: psycopg.Connection, schema: str, kind: str, local_id: str) -> str:
    row = conn.execute(f"SELECT gid FROM {db.ident(schema)}.shopify_ids WHERE kind = %s AND local_id = %s",
                       (kind, local_id)).fetchone()
    if row is None:
        raise ToolError(f"This {kind.replace('_', ' ')} is not synced with the store system.")
    return row[0]


def local_order_id(conn: psycopg.Connection, schema: str, gid: str) -> str | None:
    row = conn.execute(f"SELECT local_id FROM {db.ident(schema)}.shopify_ids WHERE kind = 'order' AND gid = %s",
                       (gid,)).fetchone()
    return row[0] if row else None


# ---------- GraphQL ----------
ORDER_CANCEL = """mutation($id: ID!, $note: String) {
  orderCancel(orderId: $id, reason: CUSTOMER, restock: true, notifyCustomer: false, staffNote: $note,
              refundMethod: {originalPaymentMethodsRefund: true}) {
    job { id } orderCancelUserErrors { field message } } }"""
ORDER_UPDATE = """mutation($input: OrderInput!) {
  orderUpdate(input: $input) { order { id } userErrors { field message } } }"""
CUSTOMER_ADDRESS_UPDATE = """mutation($cid: ID!, $aid: ID!, $address: MailingAddressInput!) {
  customerAddressUpdate(customerId: $cid, addressId: $aid, address: $address, setAsDefault: true) {
    address { id } userErrors { field message } } }"""
RETURN_CREATE = """mutation($input: ReturnInput!) {
  returnCreate(returnInput: $input) { return { id status } userErrors { field message } } }"""
EDIT_BEGIN = """mutation($id: ID!) {
  orderEditBegin(id: $id) { calculatedOrder { id lineItems(first: 100) { nodes { id quantity variant { id } } } }
                            userErrors { field message } } }"""
EDIT_SET_QTY = """mutation($id: ID!, $line: ID!) {
  orderEditSetQuantity(id: $id, lineItemId: $line, quantity: 0, restock: true) {
    calculatedOrder { id } userErrors { field message } } }"""
EDIT_ADD = """mutation($id: ID!, $variant: ID!) {
  orderEditAddVariant(id: $id, variantId: $variant, quantity: 1, allowDuplicates: true) {
    calculatedOrder { id } userErrors { field message } } }"""
EDIT_COMMIT = """mutation($id: ID!, $note: String) {
  orderEditCommit(id: $id, notifyCustomer: false, staffNote: $note) { order { id } userErrors { field message } } }"""
ORDER_LINES = """query($id: ID!) {
  order(id: $id) { lineItems(first: 100) { nodes { id currentQuantity variant { id } sku } } } }"""

JOB = "query($id: ID!) { job(id: $id) { done } }"
ORDER_CANCELLED = "query($id: ID!) { order(id: $id) { cancelledAt } }"

STAFF_NOTE = "Done by the support agent after the customer confirmed."
# Shopify's standard return reasons (same ids on every shop)
REASON_RETURN = "gid://shopify/ReturnReasonDefinition/2"    # changed-my-mind
REASON_EXCHANGE = "gid://shopify/ReturnReasonDefinition/5"  # other-reason


class ShopifyStore(Store):
    """Store whose writes go to Shopify first; the Postgres mirror only commits what Shopify accepted."""

    def __init__(self, conn: psycopg.Connection, schema: str, client: ShopifyClient):
        super().__init__(conn, schema)
        self.schema = schema
        self.shop = client

    def _gid(self, kind: str, local_id: str) -> str:
        return get_id(self.conn, self.schema, kind, local_id)

    def _through(self, local, remote) -> Any:
        """Stage the change locally (validates the business rules), then apply it in Shopify. If Shopify fails,
        the local change rolls back, so the mirror never shows a change the store didn't make."""
        with self.conn.transaction():
            result = local()
            remote()
        return result

    # ---------- writes ----------
    def cancel_pending_order(self, order_id: str, reason: str) -> dict:
        def remote():
            gid = self._gid("order", order_id)
            job = self.shop.mutate(ORDER_CANCEL, {"id": gid, "note": f"{STAFF_NOTE} Reason: {reason}."},
                                   "orderCancel")["job"]
            # orderCancel runs as a background job and can fail without a user error (e.g. a refund the payment
            # gateway declines). Wait for it and check the order really is cancelled before committing the mirror.
            for _ in range(20):
                done = self.shop.gql(JOB, {"id": job["id"]})["job"]["done"] if job else True
                if done and self.shop.gql(ORDER_CANCELLED, {"id": gid})["order"]["cancelledAt"]:
                    return
                time.sleep(0.5 if not done else 1)
            raise ShopifyError("Shopify could not cancel this order (the refund was not accepted). "
                               "Transfer the customer to a human agent.")
        return self._through(lambda: Store.cancel_pending_order(self, order_id, reason), remote)

    def modify_pending_order_address(self, order_id: str, address1: str, address2: str, city: str, state: str,
                                     country: str, zip: str) -> dict:
        def remote():
            user = self._user(self._order(order_id)["user_id"])
            addr = {"address1": address1, "address2": address2, "city": city, "state": state, "country": country,
                    "zip": zip}
            self.shop.mutate(ORDER_UPDATE, {"input": {
                "id": self._gid("order", order_id),
                "shippingAddress": address_input(addr, user["name"]["first_name"], user["name"]["last_name"])}},
                "orderUpdate")
        return self._through(lambda: Store.modify_pending_order_address(
            self, order_id, address1, address2, city, state, country, zip), remote)

    def modify_user_address(self, user_id: str, address1: str, address2: str, city: str, state: str,
                            country: str, zip: str) -> dict:
        def remote():
            user = self._user(user_id)
            addr = {"address1": address1, "address2": address2, "city": city, "state": state, "country": country,
                    "zip": zip}
            self.shop.mutate(CUSTOMER_ADDRESS_UPDATE, {
                "cid": self._gid("customer", user_id), "aid": self._gid("customer_address", user_id),
                "address": address_input(addr, user["name"]["first_name"], user["name"]["last_name"])},
                "customerAddressUpdate")
        return self._through(lambda: Store.modify_user_address(
            self, user_id, address1, address2, city, state, country, zip), remote)

    def modify_pending_order_payment(self, order_id: str, payment_method_id: str) -> dict:
        # Shopify has no API to move an order's payment to another method; the change is recorded on the order
        # (note) and in the mirror's payment history, where it would be settled by the payments team.
        def remote():
            self.shop.mutate(ORDER_UPDATE, {"input": {
                "id": self._gid("order", order_id),
                "note": f"Payment method changed to {payment_method_id} by the support agent (customer confirmed)."}},
                "orderUpdate")
        return self._through(lambda: Store.modify_pending_order_payment(self, order_id, payment_method_id), remote)

    def return_delivered_order_items(self, order_id: str, item_ids: list[str], payment_method_id: str) -> dict:
        def remote():
            self.shop.mutate(RETURN_CREATE, {"input": {
                "orderId": self._gid("order", order_id), "exchangeLineItems": [],
                "returnLineItems": [{"fulfillmentLineItemId": fli, "quantity": 1,
                                     "returnReasonDefinitionId": REASON_RETURN,
                                     "returnReasonNote": f"Refund to {payment_method_id}"}
                                    for fli in self._fulfillment_lines(order_id, item_ids)]}}, "returnCreate")
        return self._through(lambda: Store.return_delivered_order_items(self, order_id, item_ids, payment_method_id),
                             remote)

    def exchange_delivered_order_items(self, order_id: str, item_ids: list[str], new_item_ids: list[str],
                                       payment_method_id: str) -> dict:
        def remote():
            self.shop.mutate(RETURN_CREATE, {"input": {
                "orderId": self._gid("order", order_id),
                "returnLineItems": [{"fulfillmentLineItemId": fli, "quantity": 1,
                                     "returnReasonDefinitionId": REASON_EXCHANGE, "returnReasonNote": "Exchange"}
                                    for fli in self._fulfillment_lines(order_id, item_ids)],
                "exchangeLineItems": [{"variantId": self._gid("variant", n), "quantity": 1, "giftCardCodes": []}
                                      for n in new_item_ids]}}, "returnCreate")
        return self._through(lambda: Store.exchange_delivered_order_items(
            self, order_id, item_ids, new_item_ids, payment_method_id), remote)

    def modify_pending_order_items(self, order_id: str, item_ids: list[str], new_item_ids: list[str],
                                   payment_method_id: str) -> dict:
        def remote():
            order_gid = self._gid("order", order_id)
            begin = self.shop.mutate(EDIT_BEGIN, {"id": order_gid}, "orderEditBegin")["calculatedOrder"]
            lines = [n for n in begin["lineItems"]["nodes"] if n["quantity"] > 0]
            used: set[str] = set()
            for old_id, new_id in zip(item_ids, new_item_ids, strict=True):
                vgid = self._gid("variant", old_id)
                line = next((n for n in lines if (n["variant"] or {}).get("id") == vgid and n["id"] not in used), None)
                if line is None:
                    raise ShopifyError(f"Item {old_id} is not on the Shopify order.")
                used.add(line["id"])
                self.shop.mutate(EDIT_SET_QTY, {"id": begin["id"], "line": line["id"]}, "orderEditSetQuantity")
                self.shop.mutate(EDIT_ADD, {"id": begin["id"], "variant": self._gid("variant", new_id)},
                                 "orderEditAddVariant")
            self.shop.mutate(EDIT_COMMIT, {"id": begin["id"], "note": STAFF_NOTE}, "orderEditCommit")
            self._remap_line_items(order_id, order_gid)
        return self._through(lambda: Store.modify_pending_order_items(
            self, order_id, item_ids, new_item_ids, payment_method_id), remote)

    # ---------- helpers ----------
    def _fulfillment_lines(self, order_id: str, item_ids: list[str]) -> list[str]:
        order = self._order(order_id)
        taken: set[int] = set()
        out = []
        for item_id in item_ids:
            pos = next(i for i, it in enumerate(order["items"]) if it["item_id"] == item_id and i not in taken)
            taken.add(pos)
            out.append(self._gid("fulfillment_line_item", f"{order_id}|{pos}"))
        return out

    def _remap_line_items(self, order_id: str, order_gid: str) -> None:
        """After an order edit, Shopify has new line items; map each mirror position to its current line."""
        nodes = [n for n in self.shop.gql(ORDER_LINES, {"id": order_gid})["order"]["lineItems"]["nodes"]
                 if n["currentQuantity"] > 0]
        items = self._order(order_id)["items"]
        rows, used = [], set()
        for pos, it in enumerate(items):
            node = next((n for n in nodes if n["sku"] == it["item_id"] and n["id"] not in used), None)
            if node:
                used.add(node["id"])
                rows.append(("line_item", f"{order_id}|{pos}", node["id"]))
        put_ids(self.conn, self.schema, rows)


# ---------- webhooks ----------
def verify_webhook(body: bytes, signature: str, secret: str) -> bool:
    digest = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return bool(signature) and hmac.compare_digest(digest, signature)


def apply_webhook(conn: psycopg.Connection, schema: str, webhook_id: str, topic: str, payload: dict) -> str:
    """Bring the mirror in line with a change made in Shopify. Idempotent: each webhook id is applied once, and
    changes the agent already made (and mirrored) are recognized as in sync."""
    s = db.ident(schema)
    gid = payload.get("admin_graphql_api_id", "")
    order_id = local_order_id(conn, schema, gid) if gid.startswith("gid://shopify/Order/") else None
    with conn.transaction():
        new = conn.execute(f"""INSERT INTO {s}.shopify_events (webhook_id, topic, order_id, action)
                               VALUES (%s, %s, %s, 'received') ON CONFLICT DO NOTHING RETURNING 1""",
                           (webhook_id, topic, order_id)).fetchone()
        if not new:
            return "duplicate"
        action = "ignored"
        if order_id:
            order = db.get_order(conn, s, order_id)
            if topic == "orders/cancelled":
                if order["status"] == "cancelled":
                    action = "in sync"
                else:
                    conn.execute(f"UPDATE {s}.orders SET status = 'cancelled', cancel_reason = 'cancelled in Shopify',"
                                 f" updated_at = now() WHERE order_id = %s", (order_id,))
                    action = "mirror: cancelled"
            elif topic == "orders/updated" and payload.get("shipping_address"):
                a = payload["shipping_address"]
                addr = {"address1": a.get("address1") or "", "address2": a.get("address2") or "",
                        "city": a.get("city") or "", "country": "USA" if a.get("country_code") == "US"
                        else a.get("country_code") or "", "state": a.get("province_code") or "",
                        "zip": a.get("zip") or ""}
                if addr == order["address"]:
                    action = "in sync"
                else:
                    conn.execute(f"UPDATE {s}.orders SET address = %s, updated_at = now() WHERE order_id = %s",
                                 (psycopg.types.json.Jsonb(addr), order_id))
                    action = "mirror: address"
            else:
                action = "recorded"
        conn.execute(f"UPDATE {s}.shopify_events SET action = %s WHERE webhook_id = %s", (action, webhook_id))
        if action.startswith("mirror"):
            conn.execute(f"""INSERT INTO {s}.audit_log (session_id, tool, tier, args, outcome, detail)
                             VALUES ('shopify', %s, 'external', %s, 'ok', %s)""",
                         (topic, psycopg.types.json.Jsonb({"order_id": order_id}), action))
    return action


def money(x: Any) -> str:
    return str(Decimal(str(x)).quantize(Decimal("0.01")))
