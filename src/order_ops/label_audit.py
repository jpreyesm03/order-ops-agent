"""Label audit (D30 R2): does each task's expected answer follow the written policy?

Replays every task's expected write actions, in order, on τ²'s own retail database, and checks each one against the
policy rules that τ²'s tools don't enforce themselves (gift card balance, same product, original payment method,
status). A task whose expected answer breaks the policy can't be passed by a policy-following agent; such tasks are
listed in DECISIONS (D30 exclusions) before the final runs. Reads only the benchmark, never agent runs.

    uv run --group tau2 python -m order_ops.label_audit      # → evals/LABEL_AUDIT.md
"""
# ruff: noqa: I001  (tau2_adapter must be imported before any tau2 module)
from __future__ import annotations

import json
from typing import Any

from . import tau2_adapter  # noqa: F401  (sets TAU2_DATA_DIR)
from . import db

from tau2.runner import build_environment  # noqa: E402

STATUS = {"cancel_pending_order": "pending", "modify_pending_order_address": "pending",
          "modify_pending_order_items": "pending", "modify_pending_order_payment": "pending",
          "return_delivered_order_items": "delivered", "exchange_delivered_order_items": "delivered"}


def _variant(tdb: Any, item_id: str) -> tuple[str, Any] | None:
    for p in tdb.products.values():
        if item_id in p.variants:
            return p.product_id, p.variants[item_id]
    return None


def check(tdb: Any, name: str, a: dict) -> list[str]:
    """Policy problems with running this expected action on the current database."""
    issues: list[str] = []
    order = tdb.orders.get(a.get("order_id", "")) if a.get("order_id") else None
    if name in STATUS:
        if order is None:
            return [f"order {a.get('order_id')} not found"]
        if order.status != STATUS[name]:
            issues.append(f"order status is '{order.status}', policy requires '{STATUS[name]}'")
    user = tdb.users.get(order.user_id) if order else tdb.users.get(a.get("user_id", ""))
    pm_id = a.get("payment_method_id")
    pm = user.payment_methods.get(pm_id) if (user and pm_id) else None
    if pm_id and pm is None:
        issues.append(f"payment method {pm_id} is not the customer's")
    if name in ("exchange_delivered_order_items", "modify_pending_order_items") and order is not None:
        items = {i.item_id: i for i in order.items}
        diff = 0.0
        for old, new in zip(a.get("item_ids", []), a.get("new_item_ids", []), strict=False):
            o, v = items.get(old), _variant(tdb, new)
            if o is None or v is None:
                issues.append(f"item {old} or {new} not found")
                continue
            pid, var = v
            if pid != o.product_id:
                issues.append(f"{old} → {new} changes the product type")
            if not var.available:
                issues.append(f"new item {new} is not available")
            if old == new:
                issues.append(f"{old} is exchanged for the same item")
            diff += var.price - o.price
        if pm is not None and pm.source == "gift_card" and diff > 0 and pm.balance < round(diff, 2):
            issues.append(f"gift card balance ${pm.balance:.2f} < price difference ${diff:.2f}")
    if name == "return_delivered_order_items" and order is not None and pm is not None:
        original = {p.payment_method_id for p in order.payment_history if p.transaction_type == "payment"}
        if pm_id not in original and pm.source != "gift_card":
            issues.append(f"refund to {pm_id}: neither the original payment method nor a gift card")
    if name == "modify_pending_order_payment" and order is not None and pm is not None:
        original = [p.payment_method_id for p in order.payment_history if p.transaction_type == "payment"]
        if original and original[0] == pm_id:
            issues.append("new payment method is the same as the original")
        total = sum(p.amount for p in order.payment_history if p.transaction_type == "payment")
        if pm.source == "gift_card" and pm.balance < total:
            issues.append(f"gift card balance ${pm.balance:.2f} < order total ${total:.2f}")
    if name == "cancel_pending_order" and a.get("reason") not in ("no longer needed", "ordered by mistake"):
        issues.append(f"cancel reason '{a.get('reason')}' is not allowed")
    return issues


def audit() -> list[dict]:
    tasks = json.loads((db.TAU2_DIR / "tasks.json").read_text())
    found = []
    for t in tasks:
        env = build_environment("retail")  # fresh database per task
        tools = env.tools
        for a in t["evaluation_criteria"].get("actions") or []:
            name, args = a["name"], a.get("arguments") or {}
            if name not in STATUS and name != "modify_user_address":
                continue
            issues = check(tools.db, name, args)
            if issues:
                found.append({"task_id": t["id"], "action": name, "order_id": args.get("order_id"),
                              "issues": issues})
            try:
                getattr(tools, name)(**args)
            except Exception as e:  # noqa: BLE001 - τ²'s own tool refused the expected action
                found.append({"task_id": t["id"], "action": name, "order_id": args.get("order_id"),
                              "issues": [f"τ²'s tool raised: {e}"]})
                break
    return found


def main() -> None:
    found = audit()
    ids = sorted({f["task_id"] for f in found}, key=lambda x: int(x) if x.isdigit() else x)
    lines = ["# Label audit", "",
             "Generated by `python -m order_ops.label_audit`: every task's expected changes replayed in order on "
             "τ²'s retail database and checked against the written policy. Tasks listed here have an expected "
             "answer that a policy-following agent can't produce; they are excluded from the D30 bars.", "",
             f"Tasks checked: {len(json.loads((db.TAU2_DIR / 'tasks.json').read_text()))} · with a conflict: "
             f"{len(ids)} ({', '.join(ids) or 'none'})", "",
             "| Task | Expected action | Order | Problem |", "|---|---|---|---|"]
    for f in found:
        lines.append(f"| {f['task_id']} | {f['action']} | {f['order_id'] or ''} | {'; '.join(f['issues'])} |")
    (db.ROOT / "evals" / "LABEL_AUDIT.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
