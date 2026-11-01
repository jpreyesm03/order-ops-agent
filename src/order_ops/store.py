"""Store tools: the 15 retail actions, same contract as τ²-bench retail, implemented over Postgres.

These functions enforce *data* rules only (order status, item counts, gift-card balance). The *policy* rules
(identity, ownership, confirmation, approvals, idempotency) live in gate.py, one layer above.
Each write runs in one transaction: it either fully applies or not at all.
"""
from __future__ import annotations

import ast
import json
import operator
import unicodedata
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import db


class ToolError(ValueError):
    """A tool refused the call (bad input or a data rule). The message goes back to the model."""


CANCEL_REASONS = {"no longer needed", "ordered by mistake"}


def fold(text: str) -> str:
    """Accent- and case-insensitive match key (same rule as τ²)."""
    decomposed = unicodedata.normalize("NFD", text.strip())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _arith(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _arith(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_arith(node.left), _arith(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_arith(node.operand))
    raise ToolError("Invalid expression")


class Store:
    def __init__(self, conn: psycopg.Connection, schema: str):
        self.conn = conn
        self.s = db.ident(schema)

    # ---------- helpers ----------
    def _order(self, order_id: str) -> dict:
        o = db.get_order(self.conn, self.s, order_id)
        if o is None:
            raise ToolError("Order not found")
        return o

    def _user(self, user_id: str) -> dict:
        u = db.get_user(self.conn, self.s, user_id)
        if u is None:
            raise ToolError("User not found")
        return u

    def _payment_method(self, user_id: str, pm_id: str) -> dict:
        pm = self._user(user_id)["payment_methods"].get(pm_id)
        if pm is None:
            raise ToolError("Payment method not found")
        return pm

    def _variant(self, product_id: str, item_id: str) -> dict:
        p = db.get_product(self.conn, self.s, product_id)
        if p is None:
            raise ToolError("Product not found")
        v = p["variants"].get(item_id)
        if v is None:
            raise ToolError("Variant not found")
        return v

    def _adjust_gift_card(self, user_id: str, pm_id: str, delta: Decimal) -> None:
        self.conn.execute(
            f"UPDATE {self.s}.payment_methods SET balance = balance + %s "
            f"WHERE user_id = %s AND payment_method_id = %s AND source = 'gift_card'",
            (delta, user_id, pm_id),
        )

    def _add_payment(self, order_id: str, ttype: str, amount: Decimal, pm_id: str) -> None:
        self.conn.execute(
            f"""INSERT INTO {self.s}.order_payments
                SELECT %s, COALESCE(MAX(position) + 1, 0), %s, %s, %s
                FROM {self.s}.order_payments WHERE order_id = %s""",
            (order_id, ttype, amount, pm_id, order_id),
        )

    @staticmethod
    def _check_counts(order: dict, item_ids: list[str], message: str) -> None:
        have = [it["item_id"] for it in order["items"]]
        for item_id in item_ids:
            if item_ids.count(item_id) > have.count(item_id):
                raise ToolError(message.format(item_id=item_id))

    # ---------- read tools ----------
    def find_user_id_by_email(self, email: str) -> str:
        rows = [r[0] for r in self.conn.execute(f"SELECT user_id, email FROM {self.s}.users")
                if fold(r[1]) == fold(email)] if email else []
        if len(rows) == 1:
            return rows[0]
        raise ToolError("User lookup is ambiguous" if rows else "User not found")

    def find_user_id_by_name_zip(self, first_name: str, last_name: str, zip: str) -> str:
        rows = [uid for uid, f, last, z in self.conn.execute(
            f"SELECT user_id, first_name, last_name, address->>'zip' FROM {self.s}.users")
            if fold(f) == fold(first_name) and fold(last) == fold(last_name) and z == zip]
        if len(rows) == 1:
            return rows[0]
        raise ToolError("User lookup is ambiguous" if rows else "User not found")

    def get_user_details(self, user_id: str) -> dict:
        return self._user(user_id)

    def get_order_details(self, order_id: str) -> dict:
        return self._order(order_id)

    def get_product_details(self, product_id: str) -> dict:
        p = db.get_product(self.conn, self.s, product_id)
        if p is None:
            raise ToolError("Product not found")
        return p

    def get_item_details(self, item_id: str) -> dict:
        row = self.conn.execute(
            f"SELECT item_id, options, available, price FROM {self.s}.variants WHERE item_id = %s LIMIT 1",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ToolError("Item not found")
        return {"item_id": row[0], "options": row[1], "available": row[2], "price": db.money(row[3])}

    def list_all_product_types(self) -> str:
        rows = self.conn.execute(f"SELECT name, product_id FROM {self.s}.products").fetchall()
        return json.dumps(dict(rows), sort_keys=True)

    def calculate(self, expression: str) -> str:
        if not all(ch in "0123456789+-*/(). " for ch in expression):
            raise ToolError("Invalid characters in expression")
        try:
            value = _arith(ast.parse(expression, mode="eval"))
        except (SyntaxError, ZeroDivisionError) as e:
            raise ToolError(f"Invalid expression: {e}") from e
        return str(round(float(value), 2))

    def transfer_to_human_agents(self, summary: str) -> str:
        return "Transfer successful"

    # ---------- write tools ----------
    def cancel_pending_order(self, order_id: str, reason: str) -> dict:
        with self.conn.transaction():
            order = self._order(order_id)
            if order["status"] != "pending":
                raise ToolError("Non-pending order cannot be cancelled")
            if reason not in CANCEL_REASONS:
                raise ToolError("Invalid reason")
            for p in list(order["payment_history"]):
                amount = Decimal(str(p["amount"]))
                pm = self._payment_method(order["user_id"], p["payment_method_id"])
                self._add_payment(order_id, "refund", amount, p["payment_method_id"])
                if pm["source"] == "gift_card":
                    self._adjust_gift_card(order["user_id"], p["payment_method_id"], amount)
            self.conn.execute(
                f"UPDATE {self.s}.orders SET status = 'cancelled', cancel_reason = %s, updated_at = now() "
                f"WHERE order_id = %s", (reason, order_id))
            return self._order(order_id)

    def exchange_delivered_order_items(self, order_id: str, item_ids: list[str], new_item_ids: list[str],
                                       payment_method_id: str) -> dict:
        with self.conn.transaction():
            order = self._order(order_id)
            if order["status"] != "delivered":
                raise ToolError("Non-delivered order cannot be exchanged")
            self._check_counts(order, item_ids, "Number of {item_id} not found.")
            if len(item_ids) != len(new_item_ids):
                raise ToolError("The number of items to be exchanged should match.")
            diff = Decimal("0")
            for old_id, new_id in zip(item_ids, new_item_ids, strict=True):
                item = next(it for it in order["items"] if it["item_id"] == old_id)
                variant = self._variant(item["product_id"], new_id)
                if not variant["available"]:
                    raise ToolError(f"New item {new_id} not found or available")
                diff += Decimal(str(variant["price"])) - Decimal(str(item["price"]))
            pm = self._payment_method(order["user_id"], payment_method_id)
            if pm["source"] == "gift_card" and Decimal(str(pm["balance"])) < diff:
                raise ToolError("Insufficient gift card balance to pay for the price difference")
            self.conn.execute(
                f"""UPDATE {self.s}.orders SET status = 'exchange requested', exchange_items = %s,
                    exchange_new_items = %s, exchange_payment_method_id = %s, exchange_price_difference = %s,
                    updated_at = now() WHERE order_id = %s""",
                (sorted(item_ids), sorted(new_item_ids), payment_method_id, diff, order_id))
            return self._order(order_id)

    def modify_pending_order_address(self, order_id: str, address1: str, address2: str, city: str, state: str,
                                     country: str, zip: str) -> dict:
        with self.conn.transaction():
            order = self._order(order_id)
            if "pending" not in order["status"]:
                raise ToolError("Non-pending order cannot be modified")
            address = {"address1": address1, "address2": address2, "city": city, "country": country,
                       "state": state, "zip": zip}
            self.conn.execute(f"UPDATE {self.s}.orders SET address = %s, updated_at = now() WHERE order_id = %s",
                              (Jsonb(address), order_id))
            return self._order(order_id)

    def modify_pending_order_items(self, order_id: str, item_ids: list[str], new_item_ids: list[str],
                                   payment_method_id: str) -> dict:
        with self.conn.transaction():
            order = self._order(order_id)
            if order["status"] != "pending":
                raise ToolError("Non-pending order cannot be modified")
            self._check_counts(order, item_ids, "{item_id} not found")
            if len(item_ids) != len(new_item_ids):
                raise ToolError("The number of items to be exchanged should match")
            diff = Decimal("0")
            new_variants = []
            for old_id, new_id in zip(item_ids, new_item_ids, strict=True):
                if old_id == new_id:
                    raise ToolError("The new item id should be different from the old item id")
                item = next(it for it in order["items"] if it["item_id"] == old_id)
                variant = self._variant(item["product_id"], new_id)
                if not variant["available"]:
                    raise ToolError(f"New item {new_id} not found or available")
                diff += Decimal(str(variant["price"])) - Decimal(str(item["price"]))
                new_variants.append(variant)
            pm = self._payment_method(order["user_id"], payment_method_id)
            if pm["source"] == "gift_card" and Decimal(str(pm["balance"])) < diff:
                raise ToolError("Insufficient gift card balance to pay for the new item")
            self._add_payment(order_id, "payment" if diff > 0 else "refund", abs(diff), payment_method_id)
            if pm["source"] == "gift_card":
                self._adjust_gift_card(order["user_id"], payment_method_id, -diff)
            # swap each old item for its own new variant (τ²'s reference code reuses the last variant for
            # every item; this repo fixes that, see DECISIONS.md)
            taken: set[int] = set()
            for old_id, variant in zip(item_ids, new_variants, strict=True):
                pos = next(i for i, it in enumerate(order["items"]) if it["item_id"] == old_id and i not in taken)
                taken.add(pos)
                self.conn.execute(
                    f"UPDATE {self.s}.order_items SET item_id = %s, price = %s, options = %s "
                    f"WHERE order_id = %s AND position = %s",
                    (variant["item_id"], Decimal(str(variant["price"])), Jsonb(variant["options"]), order_id, pos))
            self.conn.execute(f"UPDATE {self.s}.orders SET status = 'pending (item modified)', updated_at = now() "
                              f"WHERE order_id = %s", (order_id,))
            return self._order(order_id)

    def modify_pending_order_payment(self, order_id: str, payment_method_id: str) -> dict:
        with self.conn.transaction():
            order = self._order(order_id)
            if "pending" not in order["status"]:
                raise ToolError("Non-pending order cannot be modified")
            pm = self._payment_method(order["user_id"], payment_method_id)
            history = order["payment_history"]
            if len(history) != 1 or history[0]["transaction_type"] != "payment":
                raise ToolError("There should be exactly one payment for a pending order")
            old_pm_id = history[0]["payment_method_id"]
            if old_pm_id == payment_method_id:
                raise ToolError("The new payment method should be different from the current one")
            amount = Decimal(str(history[0]["amount"]))
            if pm["source"] == "gift_card" and Decimal(str(pm["balance"])) < amount:
                raise ToolError("Insufficient gift card balance to pay for the order")
            self._add_payment(order_id, "payment", amount, payment_method_id)
            self._add_payment(order_id, "refund", amount, old_pm_id)
            if pm["source"] == "gift_card":
                self._adjust_gift_card(order["user_id"], payment_method_id, -amount)
            if self._payment_method(order["user_id"], old_pm_id)["source"] == "gift_card":
                self._adjust_gift_card(order["user_id"], old_pm_id, amount)
            return self._order(order_id)

    def modify_user_address(self, user_id: str, address1: str, address2: str, city: str, state: str,
                            country: str, zip: str) -> dict:
        with self.conn.transaction():
            self._user(user_id)
            address = {"address1": address1, "address2": address2, "city": city, "country": country,
                       "state": state, "zip": zip}
            self.conn.execute(f"UPDATE {self.s}.users SET address = %s WHERE user_id = %s", (Jsonb(address), user_id))
            return self._user(user_id)

    def return_delivered_order_items(self, order_id: str, item_ids: list[str], payment_method_id: str) -> dict:
        with self.conn.transaction():
            order = self._order(order_id)
            if order["status"] != "delivered":
                raise ToolError("Non-delivered order cannot be returned")
            pm = self._payment_method(order["user_id"], payment_method_id)
            if pm["source"] != "gift_card" and payment_method_id != order["payment_history"][0]["payment_method_id"]:
                raise ToolError("Payment method should be the original payment method")
            self._check_counts(order, item_ids, "Some item not found")
            self.conn.execute(
                f"""UPDATE {self.s}.orders SET status = 'return requested', return_items = %s,
                    return_payment_method_id = %s, updated_at = now() WHERE order_id = %s""",
                (sorted(item_ids), payment_method_id, order_id))
            return self._order(order_id)


READ_TOOLS = {"find_user_id_by_email", "find_user_id_by_name_zip", "get_user_details", "get_order_details",
              "get_product_details", "get_item_details", "list_all_product_types", "calculate",
              "transfer_to_human_agents"}
WRITE_TOOLS = {"cancel_pending_order", "exchange_delivered_order_items", "modify_pending_order_address",
               "modify_pending_order_items", "modify_pending_order_payment", "modify_user_address",
               "return_delivered_order_items"}
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS


def call(store: Store, name: str, args: dict[str, Any]) -> Any:
    """Run a tool by name without any policy checks (used by the gate, and to replay gold actions)."""
    if name not in ALL_TOOLS:
        raise ToolError(f"Unknown tool {name}")
    try:
        return getattr(store, name)(**args)
    except TypeError as e:  # wrong / missing arguments from the model
        raise ToolError(f"Bad arguments for {name}: {e}") from e
    except StopIteration as e:
        raise ToolError("Item not found") from e
