"""The τ² runner end to end with no API calls: litellm's mock response plays the customer, a stub plays the judge."""
# ruff: noqa: I001, E402  (the adapter must be imported before any tau2 module)
from __future__ import annotations

import json

import pytest

run = pytest.importorskip("order_ops.tau2_run", reason="needs the eval group: uv sync --group tau2")
from tau2.data_model.message import AssistantMessage
from tau2.data_model.simulation import TextRunConfig
from tau2.runner import get_tasks
from test_agent_loop import FakeClient, say


def test_episode_summary_and_cost_plan(monkeypatch):
    judged = []

    def fake_judge(model, messages, **kw):  # what τ²'s generate returns: a fenced JSON answer
        judged.append(model)
        body = {"results": [{"expectedOutcome": "x", "reasoning": "r", "metExpectation": True}]}
        return AssistantMessage(role="assistant", content="```json\n" + json.dumps(body) + "\n```",
                                usage={"prompt_tokens": 1000, "completion_tokens": 100})

    monkeypatch.setattr(run, "_tau2_generate", fake_judge)
    run.register_prices()
    run.use_claude_judge("claude-sonnet-5-5")
    name = run.register_agent("single", "on", "claude-haiku-5-5", FakeClient([say("Hello!")] * 5))
    config = TextRunConfig(domain="retail", agent=name, llm_agent="claude-haiku-5-5",
                           llm_user="anthropic/claude-haiku-5-5",
                           llm_args_user={"mock_response": "Thanks, that's all. ###STOP###"}, max_steps=20)
    task = get_tasks("retail", task_ids=["38"])[0]
    r = run.run_episode(config, task, 0, "claude-haiku-5-5")
    assert "error" not in r, r.get("error")
    # the customer left without the cancellation the task needs: τ²'s DB check fails; the judge still ran on Claude
    assert r["reward"] == 0.0 and r["db_match"] is False
    assert judged == ["anthropic/claude-sonnet-5-5"] and r["nl_ok"] is True
    assert r["judge_cost_usd"] == pytest.approx((1000 * 2.0 + 100 * 10.0) / 1e6)
    assert r["gate"] == "on" and r["usage"]["judge_calls"] == 1

    s = run.summarize([r, r | {"trial": 1, "reward": 1.0}], trials=2)
    assert s["pass^1"] == 0.5 and s["pass^2"] == 0.0 and s["errors"] == 0
    plan = run.plan_cost(0.01)
    assert [p["episodes"] for p in plan] == [456, 160, 160, 160]
    assert sum(p["est_usd"] for p in plan) == pytest.approx(4.56 + 1.6 + 1.84 + 1.84)


def test_shadow_counts_split_by_outcome():
    base = {"gate_stats": {"blocked": {}, "approvals": 0}, "db_match": True, "nl_ok": True, "termination": "user_stop",
            "turns": 3, "usage": {"agent": {"calls": 4}}, "agent_cost_usd": 0.01, "user_cost_usd": 0.0,
            "judge_cost_usd": 0.0, "cost_usd": 0.01}
    results = [base | {"task_id": "1", "trial": 0, "reward": 0.0,
                       "gate_stats": {"blocked": {}, "approvals": 0, "would_block": {"confirmation": 2}}},
               base | {"task_id": "2", "trial": 0, "reward": 1.0,
                       "gate_stats": {"blocked": {}, "approvals": 0, "would_block": {"identity": 1}}}]
    s = run.summarize(results, trials=1)
    assert s["would_block_if_on"] == {"confirmation": 2, "identity": 1}
    assert s["would_block_in_failed_episodes"] == 2 and s["would_block_in_passed_episodes"] == 1
