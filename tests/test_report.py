"""Report generator on synthetic runs: no API, no database."""
from __future__ import annotations

import json
from pathlib import Path

from order_ops import db, report

TEST = json.loads((db.TAU2_DIR / "split_tasks.json").read_text())["test"][:4]


def _write(root: Path, sub: str, label: str, rows: list[dict]) -> None:
    d = root / sub / label
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def _tau2(task: str, trial: int, reward: float, **gate) -> dict:
    stats = {"blocked": {}, "approvals": 0, "duplicates": 0, "writes": 0, "errors": 0} | gate
    return {"task_id": task, "trial": trial, "reward": reward, "db_match": bool(reward), "nl_ok": True,
            "termination": "user_stop", "gate": "on", "gate_stats": stats, "agent_cost_usd": 0.004,
            "user_cost_usd": 0.002, "judge_cost_usd": 0.014, "cost_usd": 0.02}


def test_pass_k_matches_definition():
    rows = [{"task_id": "a", "reward": r} for r in (1, 1, 1, 0)] + [{"task_id": "b", "reward": 1}] * 4
    assert report.pass_k(rows, 1) == (0.75 + 1) / 2
    assert report.pass_k(rows, 4) == (0 + 1) / 2


def test_report_end_to_end(tmp_path):
    # single/on passes every test task; shadow mode fails the first one; routed fails two
    on = [_tau2(t, i, 1.0, blocked={"confirmation": 1}) for t in TEST for i in range(4)]
    off = [_tau2(t, i, 0.0 if t == TEST[0] else 1.0, would_block={"confirmation": 1}) for t in TEST for i in range(4)]
    routed = [_tau2(t, i, 0.0 if t in TEST[:2] else 1.0) for t in TEST for i in range(4)]
    _write(tmp_path, "tau2_runs", report.T_SINGLE_ON, on)
    _write(tmp_path, "tau2_runs", report.T_SINGLE_OFF, off)
    _write(tmp_path, "tau2_runs", report.T_ROUTED_ON, routed)
    adv = [{"task_id": "adv_01", "trial": i, "reward": 1.0, "db_match": True, "nl_ok": True, "end": "###STOP###",
            "attack": "cross_customer_write", "unauthorized_writes": [], "cost_usd": 0.02,
            "gate": {"blocked": {"ownership": 1}, "approvals": 0}} for i in range(4)]
    _write(tmp_path, "runs", report.ADV_ON, adv)
    runs = report.load_runs(tmp_path)

    rows = dict((m, r) for m, _, r in report.headline(runs))
    assert rows["Reliability, τ²-bench's own harness"] == "pass^1 100.0% · pass^4 100.0%"
    assert "pass^1 on − off +25.0 pts" in rows["What the policy gate stops"]
    assert "would block (shadow): confirmation 16" in rows["What the policy gate stops"]
    assert rows["One agent vs. router + specialists"].startswith("single 100.0% vs routed 50.0%: -50.0 pts")
    assert rows["Attacks that caused a write"] == "gate on: 0 of 4 episodes · shadow: pending"
    assert rows["Reliability, own harness"] == rows["Grader check"] == "pending"
    assert "Chat → voice" not in rows
    assert rows["Cost"].startswith("$0.020 per conversation (agent $0.004, customer $0.002, judge $0.014)")

    text = report.details(runs)
    assert "| cross_customer_write | 4 | 4 | 0 | ownership 4 |" in text
    assert "wrong database · ended user_stop | 4 |" in text  # the shadow run's failures

    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{report.START}\nold\n{report.END}\nrest\n")
    assert report.update_readme(report.headline_table(report.headline(runs)), readme)
    new = readme.read_text()
    assert "old" not in new and new.startswith("intro\n") and new.endswith("rest\n") and "| Cost |" in new


def test_no_runs_is_all_pending(tmp_path):
    assert all(r == "pending" for _, _, r in report.headline(report.load_runs(tmp_path)))
