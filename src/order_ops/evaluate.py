"""Eval runner, τ²-bench style.

For every (task, trial):
  1. clone the pristine store into a throwaway schema
  2. the agent talks to the LLM user simulator until the customer ends the chat
  3. score: final store == store after replaying the task's gold actions (DB), and the task's natural-language
     assertions judged by an LLM (NL). reward = DB × NL
Reliability is reported as pass^k: the chance that k independent trials of a task all succeed.

    uv run python -m order_ops.evaluate --split test --trials 4 --label baseline
"""
from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import comb
from typing import Any

import anthropic

from . import db
from .agent import Agent, Usage
from .gate import Gate, Session
from .router import RoutedAgent
from .simulator import TRANSFER, UserSimulator, scenario_text
from .store import WRITE_TOOLS, Store, ToolError, call

RUNS_DIR = db.ROOT / "evals" / "runs"
MAX_TURNS = 30
GREETING = "Hi! How can I help you today?"

_gold_cache: dict[str, str] = {}
_gold_lock = threading.Lock()


def load_tasks(split: str) -> list[dict]:
    tasks = json.loads((db.TAU2_DIR / "tasks.json").read_text())
    if split == "all":
        return tasks
    ids = set(json.loads((db.TAU2_DIR / "split_tasks.json").read_text())[split])
    return [t for t in tasks if t["id"] in ids]


def ensure_base(conn) -> None:
    exists = conn.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'base'").fetchone()
    if not exists:
        db.load_store(conn, "base", db.load_tau2_db())


def gold_state(conn, task: dict) -> str:
    """Canonical store after replaying the task's gold actions on a fresh copy (cached per task)."""
    with _gold_lock:
        if task["id"] in _gold_cache:
            return _gold_cache[task["id"]]
    schema = f"gold_{uuid.uuid4().hex[:8]}"
    db.clone_schema(conn, "base", schema)
    try:
        store = Store(conn, schema)
        for action in task["evaluation_criteria"].get("actions") or []:
            try:
                call(store, action["name"], action["arguments"])
            except ToolError:
                pass  # τ² ignores failing gold actions too
        state = db.canonical(db.export_state(conn, schema))
    finally:
        db.drop_schema(conn, schema)
    with _gold_lock:
        _gold_cache[task["id"]] = state
    return state


JUDGE_SYSTEM = """You grade a customer-service conversation against a list of expected outcomes.
Grade each expected outcome individually and strictly, based only on the conversation."""


def judge_nl(client: anthropic.Anthropic, transcript: str, assertions: list[str], usage: Usage) -> list[dict]:
    if not assertions:
        return []
    model = os.getenv("JUDGE_MODEL", "claude-sonnet-5-5")
    started = time.perf_counter()
    resp = client.messages.create(
        model=model, max_tokens=4000, system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": f"<conversation>\n{transcript}\n</conversation>\n\n"
                                              f"<expected_outcomes>\n{json.dumps(assertions, indent=1)}\n"
                                              f"</expected_outcomes>"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": {
            "type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["expected_outcome", "reasoning", "met"],
                "properties": {"expected_outcome": {"type": "string"}, "reasoning": {"type": "string"},
                               "met": {"type": "boolean"}}}}}}}},
    )
    usage.add(model, resp.usage, round((time.perf_counter() - started) * 1000))
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["results"]


def run_episode(task: dict, trial: int, client: anthropic.Anthropic, threshold: float,
                arch: str = "single") -> dict[str, Any]:
    conn = db.connect()
    schema = f"ep_{uuid.uuid4().hex[:10]}"
    started = time.perf_counter()
    try:
        expected = gold_state(conn, task)
        db.clone_schema(conn, "base", schema)
        gate = Gate(conn, schema)
        session = Session(session_id=f"eval-{task['id']}-{trial}", auto_approve=True, approval_threshold=threshold)
        agent = RoutedAgent(gate, client=client) if arch == "routed" else Agent(gate, client=client)
        sim = UserSimulator(scenario_text(task), client=client)
        messages: list[dict] = []
        transcript = [f"assistant: {GREETING}"]
        tool_calls: list[dict] = []
        agent_text, end = GREETING, None
        for _ in range(MAX_TURNS):
            customer = sim.reply(agent_text)
            end = sim.ended(customer)
            if end:
                break
            transcript.append(f"user: {customer}")
            turn = agent.respond(session, messages, customer)
            tool_calls += turn.tool_calls
            agent_text = turn.reply
            transcript.append(f"assistant: {agent_text}")
            if turn.transferred:
                end = TRANSFER
                break
        actual_state = db.export_state(conn, schema)
        db_match = db.canonical(actual_state) == expected
        diff = [] if db_match else db.diff_states(json.loads(expected), actual_state)
        judge_usage = Usage()
        nl = judge_nl(client, "\n".join(transcript), task["evaluation_criteria"].get("nl_assertions") or [],
                      judge_usage)
        nl_ok = all(r["met"] for r in nl)
        gold_writes = [a["name"] for a in task["evaluation_criteria"].get("actions") or [] if a["name"] in WRITE_TOOLS]
        did_writes = [c["name"] for c in tool_calls if c["name"] in WRITE_TOOLS and c["result"].get("ok")
                      and not c["result"].get("duplicate")]
        agent_cost = agent.cost() if arch == "routed" else agent.usage.cost(agent.model)
        cost = (agent_cost + sim.usage.cost(sim.model)
                + judge_usage.cost(os.getenv("JUDGE_MODEL", "claude-sonnet-5-5")))
        return {
            "task_id": task["id"], "trial": trial, "reward": float(db_match and nl_ok), "db_match": db_match,
            "nl_ok": nl_ok, "nl": nl, "diff": diff[:20], "end": end or "max_turns",
            "turns": sum(1 for t in transcript if t.startswith("user:")),
            "gold_writes": gold_writes, "did_writes": did_writes, "gate": session.stats,
            "tool_calls": tool_calls, "transcript": transcript,
            "usage": {"agent": vars(agent.usage) | {"model": agent.model},
                      "sim": vars(sim.usage) | {"model": sim.model}, "judge": vars(judge_usage)},
            "agent_cost_usd": round(agent_cost, 5), "cost_usd": round(cost, 5), "arch": arch,
            "routes": getattr(agent, "routes", None), "handoffs": getattr(agent, "handoffs", 0),
            "agent_latency_ms_per_call": round(agent.usage.latency_ms / max(agent.usage.calls, 1)),
            "wall_s": round(time.perf_counter() - started, 1),
        }
    except Exception as e:  # noqa: BLE001 - one broken episode must not kill the run
        return {"task_id": task["id"], "trial": trial, "reward": 0.0, "error": f"{type(e).__name__}: {e}",
                "wall_s": round(time.perf_counter() - started, 1)}
    finally:
        db.drop_schema(conn, schema)
        conn.close()


def pass_hat_k(results: list[dict], k: int) -> float:
    """τ-bench pass^k: mean over tasks of C(c, k) / C(n, k), c = successes out of n trials."""
    by_task: dict[str, list[float]] = {}
    for r in results:
        by_task.setdefault(r["task_id"], []).append(r["reward"])
    vals = [comb(int(sum(v)), k) / comb(len(v), k) for v in by_task.values() if len(v) >= k]
    return sum(vals) / len(vals) if vals else float("nan")


def summarize(results: list[dict], trials: int) -> dict[str, Any]:
    ok = [r for r in results if "error" not in r]
    summary = {f"pass^{k}": round(pass_hat_k(results, k), 3) for k in range(1, trials + 1)}
    summary.update({
        "episodes": len(results), "errors": len(results) - len(ok),
        "db_match_rate": round(sum(r["db_match"] for r in ok) / max(len(ok), 1), 3),
        "nl_ok_rate": round(sum(r["nl_ok"] for r in ok) / max(len(ok), 1), 3),
        "blocked_by_gate": _sum_dicts([r["gate"]["blocked"] for r in ok]),
        "approvals_needed": sum(r["gate"]["approvals"] for r in ok),
        "duplicate_writes_stopped": sum(r["gate"]["duplicates"] for r in ok),
        "agent_cost_per_episode_usd": round(sum(r["agent_cost_usd"] for r in ok) / max(len(ok), 1), 4),
        "total_cost_usd": round(sum(r["cost_usd"] for r in ok), 3),
        "avg_agent_latency_ms_per_call": round(sum(r["agent_latency_ms_per_call"] for r in ok) / max(len(ok), 1)),
        "avg_turns": round(sum(r["turns"] for r in ok) / max(len(ok), 1), 1),
    })
    return summary


def _sum_dicts(ds: list[dict]) -> dict:
    out: dict[str, int] = {}
    for d in ds:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test", choices=["train", "test", "base", "all"])
    ap.add_argument("--tasks", help="comma-separated task ids (overrides --split)")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--label", default=time.strftime("run_%Y%m%d_%H%M"))
    ap.add_argument("--arch", default="single", choices=["single", "routed"])
    ap.add_argument("--threshold", type=float, default=float(os.getenv("APPROVAL_THRESHOLD_USD", "250")))
    args = ap.parse_args()

    tasks = load_tasks("all" if args.tasks else args.split)
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    conn = db.connect()
    ensure_base(conn)
    conn.close()

    out_dir = RUNS_DIR / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    client = anthropic.Anthropic(max_retries=8)
    jobs = [(t, i) for t in tasks for i in range(args.trials)]
    random.Random(0).shuffle(jobs)
    results: list[dict] = []
    with ThreadPoolExecutor(args.concurrency) as pool, open(out_dir / "results.jsonl", "w") as f:
        futures = [pool.submit(run_episode, t, i, client, args.threshold, args.arch) for t, i in jobs]
        for n, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            results.append(r)
            f.write(json.dumps(r, default=str) + "\n")
            f.flush()
            mark = "ERR" if "error" in r else ("PASS" if r["reward"] else "fail")
            print(f"[{n}/{len(jobs)}] task {r['task_id']} t{r['trial']} {mark}"
                  + (f" {r['error']}" if "error" in r else f" db={r['db_match']} nl={r['nl_ok']} ${r['cost_usd']}"),
                  flush=True)
    summary = summarize(results, args.trials) | {
        "label": args.label, "split": args.split if not args.tasks else f"tasks:{args.tasks}",
        "trials": args.trials, "arch": args.arch, "agent_model": os.getenv("AGENT_MODEL", "claude-haiku-5-5"),
        "agent_effort": os.getenv("AGENT_EFFORT", "medium"), "approval_threshold": args.threshold,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
