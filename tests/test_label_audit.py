"""The label audit finds expected answers that break the policy (needs the tau2 group)."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("order_ops.tau2_adapter")  # first: points τ² at this repo's data

from order_ops import db  # noqa: E402
from order_ops.label_audit import build_environment, check  # noqa: E402


def test_gift_card_short_of_the_difference_is_flagged():
    task = next(t for t in json.loads((db.TAU2_DIR / "tasks.json").read_text()) if t["id"] == "105")
    action = next(a for a in task["evaluation_criteria"]["actions"] if a["name"] == "exchange_delivered_order_items")
    issues = check(build_environment("retail").tools.db, action["name"], action["arguments"])
    assert any("gift card balance $17.00" in i for i in issues)


def test_a_normal_expected_answer_passes():
    task = next(t for t in json.loads((db.TAU2_DIR / "tasks.json").read_text()) if t["id"] == "0")
    action = next(a for a in task["evaluation_criteria"]["actions"] if a["name"] == "exchange_delivered_order_items")
    assert check(build_environment("retail").tools.db, action["name"], action["arguments"]) == []
