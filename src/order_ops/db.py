"""Postgres layer: schema, loading the store from db.json, cloning a schema per episode, exporting state.

Every store lives in its own Postgres schema with identical tables:
  base      the pristine store loaded from data/tau2/db.json (never written after load)
  live      the store behind the public demo
  ep_<id>   a throwaway copy for one eval episode (dropped afterwards)
Money is NUMERIC(12,2), never float.
"""
from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
TAU2_DIR = ROOT / "data" / "tau2"

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

DATA_TABLES = ["users", "payment_methods", "products", "variants", "orders", "order_items",
               "order_payments", "order_fulfillments"]
OPS_TABLES = ["approvals", "idempotency_keys", "audit_log", "sessions"]

SCHEMA_SQL = """
CREATE TABLE {s}.users (
    user_id     TEXT PRIMARY KEY,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    email       TEXT NOT NULL,
    address     JSONB NOT NULL,
    order_ids   TEXT[] NOT NULL DEFAULT '{{}}'
);
CREATE TABLE {s}.payment_methods (
    payment_method_id TEXT NOT NULL,
    user_id     TEXT NOT NULL REFERENCES {s}.users(user_id),
    position    INT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('credit_card', 'gift_card', 'paypal')),
    brand       TEXT,
    last_four   TEXT,
    balance     NUMERIC(12,2),
    PRIMARY KEY (user_id, payment_method_id)
);
CREATE TABLE {s}.products (
    product_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);
CREATE TABLE {s}.variants (
    product_id  TEXT NOT NULL REFERENCES {s}.products(product_id),
    item_id     TEXT NOT NULL,
    position    INT NOT NULL,
    options     JSONB NOT NULL,
    available   BOOLEAN NOT NULL,
    price       NUMERIC(12,2) NOT NULL,
    PRIMARY KEY (product_id, item_id)
);
CREATE INDEX ON {s}.variants(item_id);
CREATE TABLE {s}.orders (
    order_id    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES {s}.users(user_id),
    address     JSONB NOT NULL,
    status      TEXT NOT NULL,
    cancel_reason TEXT,
    exchange_items TEXT[],
    exchange_new_items TEXT[],
    exchange_payment_method_id TEXT,
    exchange_price_difference NUMERIC(12,2),
    return_items TEXT[],
    return_payment_method_id TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON {s}.orders(user_id);
CREATE TABLE {s}.order_items (
    order_id    TEXT NOT NULL REFERENCES {s}.orders(order_id),
    position    INT NOT NULL,
    item_id     TEXT NOT NULL,
    product_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    price       NUMERIC(12,2) NOT NULL,
    options     JSONB NOT NULL,
    PRIMARY KEY (order_id, position)
);
CREATE TABLE {s}.order_payments (
    order_id    TEXT NOT NULL REFERENCES {s}.orders(order_id),
    position    INT NOT NULL,
    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('payment', 'refund')),
    amount      NUMERIC(12,2) NOT NULL,
    payment_method_id TEXT NOT NULL,
    PRIMARY KEY (order_id, position)
);
CREATE TABLE {s}.order_fulfillments (
    order_id    TEXT NOT NULL REFERENCES {s}.orders(order_id),
    position    INT NOT NULL,
    tracking_ids TEXT[] NOT NULL,
    item_ids    TEXT[] NOT NULL,
    PRIMARY KEY (order_id, position)
);
CREATE TABLE {s}.approvals (
    approval_id TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    order_id    TEXT,
    tool        TEXT NOT NULL,
    args        JSONB NOT NULL,
    amount      NUMERIC(12,2) NOT NULL,
    summary     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'failed')),
    decided_by  TEXT,
    decision_note TEXT,
    result      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ
);
CREATE TABLE {s}.idempotency_keys (
    key         TEXT PRIMARY KEY,
    tool        TEXT NOT NULL,
    args        JSONB NOT NULL,
    result      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE {s}.audit_log (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id  TEXT NOT NULL,
    tool        TEXT NOT NULL,
    tier        TEXT NOT NULL,
    args        JSONB NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('ok', 'error', 'blocked', 'would_block', 'pending_approval',
                                                  'duplicate')),
    detail      TEXT
);
CREATE TABLE {s}.sessions (
    session_id  TEXT PRIMARY KEY,
    channel     TEXT NOT NULL DEFAULT 'web',
    state       JSONB NOT NULL,
    messages    JSONB NOT NULL DEFAULT '[]',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ident(name: str) -> str:
    """Schema names are interpolated into SQL, so only allow plain lowercase identifiers."""
    if not _IDENT.match(name):
        raise ValueError(f"bad schema name: {name!r}")
    return name


EXPECTED_DB = "orderops_p5"


def connect(url: str | None = None) -> psycopg.Connection:
    """Connect to this project's database. Refuses any other database, so a wrong URL can't touch other data."""
    load_dotenv(ROOT / ".env")
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL in .env (see .env.example).")
    conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
    name = conn.execute("SELECT current_database()").fetchone()[0]
    if name != EXPECTED_DB:
        conn.close()
        raise RuntimeError(f"Refusing to use database {name!r}: this project only runs on {EXPECTED_DB!r}.")
    return conn


def money(x: Any) -> float:
    """NUMERIC -> float rounded to cents, only at the export boundary (JSON for the model / comparisons)."""
    return float(round(Decimal(str(x)), 2))


def create_schema(conn: psycopg.Connection, schema: str) -> None:
    s = ident(schema)
    with conn.transaction():
        conn.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
        conn.execute(f"CREATE SCHEMA {s}")
        conn.execute(SCHEMA_SQL.format(s=s))


def load_store(conn: psycopg.Connection, schema: str, db: dict[str, Any]) -> None:
    """Load a τ²-shaped db dict (users / orders / products) into `schema`."""
    s = ident(schema)
    create_schema(conn, s)
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {s}.products VALUES (%s, %s)",
            [(p["product_id"], p["name"]) for p in db["products"].values()],
        )
        cur.executemany(
            f"INSERT INTO {s}.variants VALUES (%s, %s, %s, %s, %s, %s)",
            [(p["product_id"], v["item_id"], i, Jsonb(v["options"]), v["available"], Decimal(str(v["price"])))
             for p in db["products"].values() for i, v in enumerate(p["variants"].values())],
        )
        cur.executemany(
            f"INSERT INTO {s}.users VALUES (%s, %s, %s, %s, %s, %s)",
            [(u["user_id"], u["name"]["first_name"], u["name"]["last_name"], u["email"], Jsonb(u["address"]),
              u["orders"]) for u in db["users"].values()],
        )
        cur.executemany(
            f"INSERT INTO {s}.payment_methods VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(pm["id"], u["user_id"], i, pm["source"], pm.get("brand"), pm.get("last_four"),
              Decimal(str(pm["balance"])) if "balance" in pm else None)
             for u in db["users"].values() for i, pm in enumerate(u["payment_methods"].values())],
        )
        cur.executemany(
            f"""INSERT INTO {s}.orders (order_id, user_id, address, status, cancel_reason, exchange_items,
                exchange_new_items, exchange_payment_method_id, exchange_price_difference, return_items,
                return_payment_method_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(o["order_id"], o["user_id"], Jsonb(o["address"]), o["status"], o.get("cancel_reason"),
              o.get("exchange_items"), o.get("exchange_new_items"), o.get("exchange_payment_method_id"),
              Decimal(str(o["exchange_price_difference"])) if o.get("exchange_price_difference") is not None
              else None, o.get("return_items"), o.get("return_payment_method_id"))
             for o in db["orders"].values()],
        )
        cur.executemany(
            f"INSERT INTO {s}.order_items VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(o["order_id"], i, it["item_id"], it["product_id"], it["name"], Decimal(str(it["price"])),
              Jsonb(it["options"])) for o in db["orders"].values() for i, it in enumerate(o["items"])],
        )
        cur.executemany(
            f"INSERT INTO {s}.order_payments VALUES (%s, %s, %s, %s, %s)",
            [(o["order_id"], i, p["transaction_type"], Decimal(str(p["amount"])), p["payment_method_id"])
             for o in db["orders"].values() for i, p in enumerate(o["payment_history"])],
        )
        cur.executemany(
            f"INSERT INTO {s}.order_fulfillments VALUES (%s, %s, %s, %s)",
            [(o["order_id"], i, f["tracking_id"], f["item_ids"])
             for o in db["orders"].values() for i, f in enumerate(o["fulfillments"])],
        )


def clone_schema(conn: psycopg.Connection, src: str, dst: str) -> None:
    """Copy every table of `src` into a fresh schema `dst` (≈100 ms for the retail store)."""
    s, d = ident(src), ident(dst)
    create_schema(conn, d)
    with conn.transaction():
        for t in DATA_TABLES + OPS_TABLES:
            if t == "audit_log":
                conn.execute(f"INSERT INTO {d}.audit_log (ts, session_id, tool, tier, args, outcome, detail) "
                             f"SELECT ts, session_id, tool, tier, args, outcome, detail FROM {s}.audit_log")
            else:
                conn.execute(f"INSERT INTO {d}.{t} SELECT * FROM {s}.{t}")


def drop_schema(conn: psycopg.Connection, schema: str) -> None:
    conn.execute(f"DROP SCHEMA IF EXISTS {ident(schema)} CASCADE")


def load_tau2_db() -> dict[str, Any]:
    return json.loads((TAU2_DIR / "db.json").read_text())


# ---------- export: Postgres rows -> τ²-shaped JSON (for the model, the UI and state comparison) ----------

def _user_dicts(conn: psycopg.Connection, s: str, where: str = "", params: tuple = ()) -> dict[str, dict]:
    users: dict[str, dict] = {}
    for uid, first, last, email, address, order_ids in conn.execute(
        f"SELECT user_id, first_name, last_name, email, address, order_ids FROM {s}.users {where} ORDER BY user_id",
        params,
    ):
        users[uid] = {"user_id": uid, "name": {"first_name": first, "last_name": last}, "address": address,
                      "email": email, "payment_methods": {}, "orders": list(order_ids)}
    if not users:
        return users
    for uid, pmid, source, brand, last_four, balance in conn.execute(
        f"SELECT user_id, payment_method_id, source, brand, last_four, balance FROM {s}.payment_methods "
        f"WHERE user_id = ANY(%s) ORDER BY user_id, position", (list(users),),
    ):
        pm: dict[str, Any] = {"source": source, "id": pmid}
        if brand is not None:
            pm["brand"] = brand
        if last_four is not None:
            pm["last_four"] = last_four
        if balance is not None:
            pm["balance"] = money(balance)
        users[uid]["payment_methods"][pmid] = pm
    return users


def _order_dicts(conn: psycopg.Connection, s: str, where: str = "", params: tuple = ()) -> dict[str, dict]:
    orders: dict[str, dict] = {}
    for row in conn.execute(
        f"""SELECT order_id, user_id, address, status, cancel_reason, exchange_items, exchange_new_items,
                   exchange_payment_method_id, exchange_price_difference, return_items, return_payment_method_id
            FROM {s}.orders {where} ORDER BY order_id""", params,
    ):
        (oid, uid, address, status, cancel_reason, ex_items, ex_new, ex_pm, ex_diff, ret_items, ret_pm) = row
        o: dict[str, Any] = {"order_id": oid, "user_id": uid, "address": address, "items": [], "status": status,
                             "fulfillments": [], "payment_history": []}
        optional = {"cancel_reason": cancel_reason, "exchange_items": ex_items, "exchange_new_items": ex_new,
                    "exchange_payment_method_id": ex_pm,
                    "exchange_price_difference": money(ex_diff) if ex_diff is not None else None,
                    "return_items": ret_items, "return_payment_method_id": ret_pm}
        o.update({k: v for k, v in optional.items() if v is not None})
        orders[oid] = o
    if not orders:
        return orders
    ids = (list(orders),)
    for oid, item_id, pid, name, price, options in conn.execute(
        f"SELECT order_id, item_id, product_id, name, price, options FROM {s}.order_items "
        f"WHERE order_id = ANY(%s) ORDER BY order_id, position", ids,
    ):
        orders[oid]["items"].append({"name": name, "product_id": pid, "item_id": item_id, "price": money(price),
                                     "options": options})
    for oid, ttype, amount, pmid in conn.execute(
        f"SELECT order_id, transaction_type, amount, payment_method_id FROM {s}.order_payments "
        f"WHERE order_id = ANY(%s) ORDER BY order_id, position", ids,
    ):
        orders[oid]["payment_history"].append({"transaction_type": ttype, "amount": money(amount),
                                               "payment_method_id": pmid})
    for oid, tracking, item_ids in conn.execute(
        f"SELECT order_id, tracking_ids, item_ids FROM {s}.order_fulfillments "
        f"WHERE order_id = ANY(%s) ORDER BY order_id, position", ids,
    ):
        orders[oid]["fulfillments"].append({"tracking_id": list(tracking), "item_ids": list(item_ids)})
    return orders


def _product_dicts(conn: psycopg.Connection, s: str, where: str = "", params: tuple = ()) -> dict[str, dict]:
    products = {pid: {"name": name, "product_id": pid, "variants": {}} for pid, name in
                conn.execute(f"SELECT product_id, name FROM {s}.products {where} ORDER BY product_id", params)}
    if not products:
        return products
    for pid, item_id, options, available, price in conn.execute(
        f"SELECT product_id, item_id, options, available, price FROM {s}.variants "
        f"WHERE product_id = ANY(%s) ORDER BY product_id, position", (list(products),),
    ):
        products[pid]["variants"][item_id] = {"item_id": item_id, "options": options, "available": available,
                                              "price": money(price)}
    return products


def get_user(conn, schema: str, user_id: str) -> dict | None:
    return _user_dicts(conn, ident(schema), "WHERE user_id = %s", (user_id,)).get(user_id)


def get_order(conn, schema: str, order_id: str) -> dict | None:
    return _order_dicts(conn, ident(schema), "WHERE order_id = %s", (order_id,)).get(order_id)


def get_product(conn, schema: str, product_id: str) -> dict | None:
    return _product_dicts(conn, ident(schema), "WHERE product_id = %s", (product_id,)).get(product_id)


def export_state(conn: psycopg.Connection, schema: str) -> dict[str, Any]:
    """The whole store as τ²-shaped JSON. Two stores are equal iff their canonical dumps are equal."""
    s = ident(schema)
    return {"products": _product_dicts(conn, s), "users": _user_dicts(conn, s), "orders": _order_dicts(conn, s)}


def canonical(state: dict[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def diff_states(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Human-readable list of records that differ (for failure analysis, not for scoring)."""
    out = []
    for table in ("users", "orders", "products"):
        e, a = expected.get(table, {}), actual.get(table, {})
        for key in sorted(set(e) | set(a)):
            if canonical(e.get(key) or {}) != canonical(a.get(key) or {}):
                out.append(f"{table}/{key}")
    return out
