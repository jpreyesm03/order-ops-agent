"""Seed the Shopify dev store from τ²-bench and build the Postgres mirror from the same data.

    uv run python -m order_ops.seed_shopify --users 8            # first seed
    uv run python -m order_ops.seed_shopify --users 8 --reset    # delete the seeded orders, seed again

Picks customers that have both a pending and a delivered order (so every demo flow is possible), copies them, their
orders and every product those orders touch into Shopify, then loads the same slice into the mirror schema with the
local-id → Shopify-GID map. The mirror is built in a staging schema and swapped in at the end, so the live demo
never sees a half-seeded store.
"""
from __future__ import annotations

import argparse
import hashlib
import time
from typing import Any

from . import db
from .shopify import (
    CUSTOMER_ADDRESS_UPDATE,
    MIRROR_SQL,
    ShopifyClient,
    ShopifyError,
    address_input,
    get_id,
    money,
    put_ids,
)

TAG = "order-ops-seed"
SEED_STATUSES = {"pending", "processed", "delivered"}

PRODUCT_SET = """mutation($input: ProductSetInput!, $handle: String!) {
  productSet(input: $input, synchronous: true, identifier: {handle: $handle}) {
    product { id variants(first: 100) { nodes { id sku } } } userErrors { field message } } }"""
CUSTOMER_FIND = """query($q: String!) { customers(first: 1, query: $q) { nodes { id
  defaultAddress { id address1 address2 city provinceCode zip }
  addressesV2(first: 20) { nodes { id address1 address2 city provinceCode zip } } } } }"""
SET_DEFAULT = """mutation($cid: ID!, $aid: ID!) { customerUpdateDefaultAddress(customerId: $cid, addressId: $aid) {
  customer { id } userErrors { field message } } }"""
ADDRESS_FIELDS = ("address1", "address2", "city", "provinceCode", "zip")


def same_address(a: dict, b: dict) -> bool:
    return all((a.get(k) or "") == (b.get(k) or "") for k in ADDRESS_FIELDS)
CUSTOMER_CREATE = """mutation($input: CustomerInput!) {
  customerCreate(input: $input) { customer { id } userErrors { field message } } }"""
ADDRESS_CREATE = """mutation($cid: ID!, $address: MailingAddressInput!) {
  customerAddressCreate(customerId: $cid, address: $address, setAsDefault: true) {
    address { id } userErrors { field message } } }"""
ORDER_CREATE = """mutation($order: OrderCreateOrderInput!) {
  orderCreate(order: $order, options: {inventoryBehaviour: BYPASS, sendReceipt: false,
                                       sendFulfillmentReceipt: false}) {
    order { id lineItems(first: 100) { nodes { id sku } }
            fulfillments(first: 5) { fulfillmentLineItems(first: 100) { nodes { id lineItem { id } } } } }
    userErrors { field message } } }"""
ADDRESS_UPDATE = CUSTOMER_ADDRESS_UPDATE
ORDER_DELETE = """mutation($id: ID!) { orderDelete(orderId: $id) { deletedId userErrors { field message } } }"""
SEEDED_ORDERS = """query($q: String!, $after: String) { orders(first: 50, query: $q, after: $after) {
  nodes { id } pageInfo { hasNextPage endCursor } } }"""
LOCATION = """{ productVariants(first: 1) { nodes { inventoryItem { inventoryLevels(first: 1) {
  nodes { location { id } } } } } } }"""


def pick_slice(tau: dict[str, Any], n_users: int) -> dict[str, Any]:
    """Deterministic slice of τ²: n customers with a pending and a delivered order, their seedable orders, and
    every product those orders use."""
    def seedable(o: dict) -> bool:
        ids = [it["item_id"] for it in o["items"]]
        return o["status"] in SEED_STATUSES and len(ids) == len(set(ids))

    users = []
    for uid in sorted(tau["users"], key=lambda u: hashlib.md5(u.encode()).hexdigest()):
        orders = [tau["orders"][oid] for oid in tau["users"][uid]["orders"] if seedable(tau["orders"][oid])]
        statuses = {o["status"] for o in orders}
        if {"pending", "delivered"} <= statuses and len(orders) <= 5:
            users.append((uid, orders))
        if len(users) == n_users:
            break
    out_users, out_orders, product_ids = {}, {}, set()
    for uid, orders in users:
        u = dict(tau["users"][uid])
        u["orders"] = [o["order_id"] for o in orders]
        out_users[uid] = u
        for o in orders:
            out_orders[o["order_id"]] = o
            product_ids |= {it["product_id"] for it in o["items"]}
    products = {pid: tau["products"][pid] for pid in sorted(product_ids)}
    return {"users": out_users, "orders": out_orders, "products": products}


def shopify_options(options: dict[str, str]) -> list[tuple[str, str]]:
    """Shopify allows 3 options per product: keep the first two, merge the rest into one."""
    items = list(options.items())
    if len(items) <= 3:
        return items
    rest = items[2:]
    return items[:2] + [(" and ".join(k for k, _ in rest), ", ".join(v for _, v in rest))]


def seed_products(shop: ShopifyClient, products: dict) -> list[tuple[str, str, str]]:
    rows = []
    for pid, p in products.items():
        variants = list(p["variants"].values())
        names = [k for k, _ in shopify_options(variants[0]["options"])]
        values: dict[str, list[str]] = {n: [] for n in names}
        for v in variants:
            for k, val in shopify_options(v["options"]):
                if val not in values[k]:
                    values[k].append(val)
        handle = f"tau2-{pid}"
        out = shop.mutate(PRODUCT_SET, {"handle": handle, "input": {
            "title": p["name"], "handle": handle, "vendor": "Order Ops Demo", "status": "ACTIVE", "tags": [TAG],
            "productOptions": [{"name": n, "values": [{"name": x} for x in values[n]]} for n in names],
            "variants": [{"sku": v["item_id"], "price": money(v["price"]), "inventoryPolicy": "CONTINUE",
                          "optionValues": [{"optionName": k, "name": val} for k, val in shopify_options(v["options"])]}
                         for v in variants]}}, "productSet")["product"]
        rows.append(("product", pid, out["id"]))
        rows += [("variant", n["sku"], n["id"]) for n in out["variants"]["nodes"]]
        print(f"  product {p['name']} ({len(variants)} variants)")
    return rows


def seed_customers(shop: ShopifyClient, users: dict) -> list[tuple[str, str, str]]:
    rows = []
    for uid, u in users.items():
        found = shop.gql(CUSTOMER_FIND, {"q": f"email:{u['email']}"})["customers"]["nodes"]
        address = address_input(u["address"], u["name"]["first_name"], u["name"]["last_name"])
        if found and found[0]["defaultAddress"]:  # re-seed: make the τ² address the default again
            cid, current = found[0]["id"], found[0]["defaultAddress"]
            match = next((a for a in found[0]["addressesV2"]["nodes"] if same_address(a, address)), None)
            # Order shipping addresses get added to the customer's address book (and can become the default),
            # and Shopify rejects editing an address into a duplicate of another ("Address already exists").
            if match:
                aid = match["id"]
                if aid != current["id"]:
                    shop.mutate(SET_DEFAULT, {"cid": cid, "aid": aid}, "customerUpdateDefaultAddress")
            else:
                aid = current["id"]
                shop.mutate(ADDRESS_UPDATE, {"cid": cid, "aid": aid, "address": address}, "customerAddressUpdate")
            rows += [("customer", uid, cid), ("customer_address", uid, aid)]
            print(f"  customer {u['name']['first_name']} {u['name']['last_name']} (existing)")
            continue
        if found:
            cid = found[0]["id"]
        else:
            cid = shop.mutate(CUSTOMER_CREATE, {"input": {
                "email": u["email"], "firstName": u["name"]["first_name"], "lastName": u["name"]["last_name"],
                "tags": [TAG], "note": f"τ²-bench user {uid}"}}, "customerCreate")["customer"]["id"]
        aid = shop.mutate(ADDRESS_CREATE, {"cid": cid, "address": address},
                          "customerAddressCreate")["address"]["id"]
        rows += [("customer", uid, cid), ("customer_address", uid, aid)]
        print(f"  customer {u['name']['first_name']} {u['name']['last_name']}")
    return rows


def seed_orders(shop: ShopifyClient, data: dict, ids: dict[tuple[str, str], str],
                location: str) -> list[tuple[str, str, str]]:
    rows = []
    for oid, o in data["orders"].items():
        u = data["users"][o["user_id"]]
        paid = sum(float(p["amount"]) for p in o["payment_history"] if p["transaction_type"] == "payment")
        order: dict[str, Any] = {
            "name": oid, "currency": "USD", "email": u["email"], "test": True, "tags": [TAG, o["status"]],
            "customer": {"toAssociate": {"id": ids[("customer", o["user_id"])]}},
            "shippingAddress": address_input(o["address"], u["name"]["first_name"], u["name"]["last_name"]),
            "lineItems": [{"variantId": ids[("variant", it["item_id"])], "quantity": 1,
                           "priceSet": {"shopMoney": {"amount": money(it["price"]), "currencyCode": "USD"}}}
                          for it in o["items"]],
            "transactions": [{"kind": "SALE", "status": "SUCCESS", "gateway": "manual", "test": True,
                              "amountSet": {"shopMoney": {"amount": money(paid), "currencyCode": "USD"}}}],
            "note": f"Paid with {o['payment_history'][0]['payment_method_id']} (τ²-bench order)",
        }
        if o["status"] in ("processed", "delivered"):
            track = (o.get("fulfillments") or [{}])[0].get("tracking_id", [""])
            order["fulfillmentStatus"] = "FULFILLED"
            order["fulfillment"] = {"locationId": location, "notifyCustomer": False,
                                    "shipmentStatus": "DELIVERED" if o["status"] == "delivered" else "IN_TRANSIT",
                                    "trackingNumber": track[0] if track else ""}
        out = create_order(shop, order)
        rows.append(("order", oid, out["id"]))
        lines = out["lineItems"]["nodes"]
        fli_by_line = {n["lineItem"]["id"]: n["id"] for f in out["fulfillments"]
                       for n in f["fulfillmentLineItems"]["nodes"]}
        used: set[str] = set()
        for pos, it in enumerate(o["items"]):
            line = next(n for n in lines if n["sku"] == it["item_id"] and n["id"] not in used)
            used.add(line["id"])
            rows.append(("line_item", f"{oid}|{pos}", line["id"]))
            if line["id"] in fli_by_line:
                rows.append(("fulfillment_line_item", f"{oid}|{pos}", fli_by_line[line["id"]]))
        print(f"  order {oid} {o['status']} ({len(o['items'])} items)")
    return rows


def create_order(shop: ShopifyClient, order: dict) -> dict:
    """Dev stores allow only a few new orders per minute: wait and retry when Shopify says so."""
    for attempt in range(10):
        try:
            return shop.mutate(ORDER_CREATE, {"order": order}, "orderCreate")["order"]
        except ShopifyError as e:
            if "Too many attempts" not in str(e) or attempt == 9:
                raise
            print("  (order rate limit, waiting 30 s)")
            time.sleep(30)
    raise AssertionError("unreachable")


def delete_seeded_orders(shop: ShopifyClient, keep: frozenset[str] = frozenset()) -> None:
    """Delete every order carrying the seed tag except `keep` (also catches orders left by an interrupted run)."""
    deleted, after = 0, None
    while True:
        page = shop.gql(SEEDED_ORDERS, {"q": f"tag:{TAG}", "after": after})["orders"]
        for n in page["nodes"]:
            if n["id"] not in keep:
                shop.mutate(ORDER_DELETE, {"id": n["id"]}, "orderDelete")
                deleted += 1
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    print(f"  deleted {deleted} previously seeded orders")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=8)
    ap.add_argument("--schema", default="shop")
    ap.add_argument("--reset", action="store_true",
                    help="replace previously seeded orders (new ones are created and swapped in before old ones go)")
    args = ap.parse_args()

    started = time.time()
    conn = db.connect()  # also loads .env
    shop = ShopifyClient()
    data = pick_slice(db.load_tau2_db(), args.users)
    print(f"slice: {len(data['users'])} customers, {len(data['orders'])} orders, {len(data['products'])} products")
    location = shop.gql(LOCATION)["productVariants"]["nodes"][0]["inventoryItem"]["inventoryLevels"]["nodes"][0][
        "location"]["id"]
    rows = seed_products(shop, data["products"])
    rows += seed_customers(shop, data["users"])
    ids = {(k, local): gid for k, local, gid in rows}
    rows += seed_orders(shop, data, ids, location)

    staging = f"{args.schema}_staging"
    db.load_store(conn, staging, data)
    conn.execute(MIRROR_SQL.format(s=db.ident(staging)))
    put_ids(conn, staging, rows)
    with conn.transaction():
        conn.execute(f"DROP SCHEMA IF EXISTS {db.ident(args.schema)} CASCADE")
        conn.execute(f"ALTER SCHEMA {db.ident(staging)} RENAME TO {db.ident(args.schema)}")
    get_id(conn, args.schema, "order", next(iter(data["orders"])))  # sanity check
    if args.reset:  # only now: if anything above failed, the previous demo is still intact
        delete_seeded_orders(shop, keep=frozenset(gid for k, _, gid in rows if k == "order"))
    conn.close()
    print(f"done in {time.time() - started:.0f} s: {len(rows)} ids mapped, mirror schema '{args.schema}'")


if __name__ == "__main__":
    main()
