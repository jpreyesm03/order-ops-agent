"""Severity classes: what kind of failure an episode was."""
from order_ops.severity import classify

TASK = {"evaluation_criteria": {"actions": [
    {"name": "get_order_details", "arguments": {"order_id": "#W1"}},
    {"name": "return_delivered_order_items",
     "arguments": {"order_id": "#W1", "item_ids": ["a", "b"], "payment_method_id": "pm"}}]}}


def call(name, ok=True, **args):  # noqa: ANN001, ANN201
    return {"name": name, "args": args, "result": {"ok": ok}}


def row(calls, reward=0.0, db=False, nl=True, end="###STOP###"):  # noqa: ANN001, ANN201
    return {"task_id": "1", "trial": 0, "reward": reward, "db_match": db, "nl_ok": nl, "end": end,
            "tool_calls": calls}


def test_pass_and_harmful_kinds():
    good = call("return_delivered_order_items", order_id="#W1", item_ids=["b", "a"], payment_method_id="pm")
    assert classify(row([good], reward=1.0, db=True), TASK, "own")["kind"] == "pass"
    wrong = call("return_delivered_order_items", order_id="#W1", item_ids=["a"], payment_method_id="pm")
    assert classify(row([wrong]), TASK, "own") == {"kind": "harmful", "detail": "wrong_detail", "transferred": False}
    other = call("cancel_pending_order", order_id="#W9", reason="no longer needed")
    assert classify(row([good, other]), TASK, "own")["detail"] == "unrequested"


def test_blocked_writes_dont_count_and_safe_failures():
    blocked = {"name": "cancel_pending_order", "args": {"order_id": "#W9"},
               "result": {"ok": False, "blocked_by_policy": "confirmation"}}
    c = classify(row([blocked, call("transfer_to_human_agents", summary="x")], end="###TRANSFER###"), TASK, "own")
    assert c == {"kind": "incomplete", "detail": "transferred", "transferred": True}
    good = call("return_delivered_order_items", order_id="#W1", item_ids=["a", "b"], payment_method_id="pm")
    assert classify(row([good], db=True, nl=False), TASK, "own")["kind"] == "communication"


def test_tau2_rows_use_names_only():
    r = {"task_id": "1", "trial": 0, "reward": 0.0, "db_match": False, "termination": "USER_STOP",
         "tool_calls": [{"name": "return_delivered_order_items", "ran_in_tau2": True, "ok": True, "blocked": None}]}
    assert classify(r, TASK, "tau2")["kind"] == "other"
