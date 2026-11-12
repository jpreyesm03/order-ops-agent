"""Run this repo's agent on τ²-bench retail with τ²'s own orchestrator, user simulator and evaluator (D21–D23).

Custom configuration, not a leaderboard submission (D22): the agent is this repo's (prompt, tools, loop, router,
policy gate), and the simulated customer and the natural-language judge are Claude models instead of τ²'s OpenAI
defaults. Grading is τ²'s: final database (replayed from the tool calls in the transcript) × NL assertions.

    uv sync --group tau2
    # 1. sample first: measures cost per episode and prints what the full plan would cost
    uv run --group tau2 python -m order_ops.tau2_run --split test --sample 5 --label tau2_sample
    # 2. then the plan (see PLAN below), one configuration at a time
    uv run --group tau2 python -m order_ops.tau2_run --split base --trials 4 --arch single --gate on \
        --label tau2_single_on

Results: evals/tau2_runs/<label>/results.jsonl (one line per episode) and summary.json.
"""
# ruff: noqa: I001  (import order matters: tau2_adapter must be imported before any tau2 module)
from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from typing import Any

from . import tau2_adapter  # noqa: F401  (sets TAU2_DATA_DIR before τ² loads anything)
from . import db, runlog, tracing
from .agent import PRICES, make_client
from .evaluate import pass_hat_k

import anthropic  # noqa: E402
import litellm  # noqa: E402
import tau2.evaluator.evaluator_nl_assertions as nl_module  # noqa: E402
from tau2.data_model.message import UserMessage  # noqa: E402
from tau2.data_model.simulation import TextRunConfig  # noqa: E402
from tau2.evaluator.evaluator import EvaluationType  # noqa: E402
from tau2.registry import registry  # noqa: E402
from tau2.runner import build_text_orchestrator, get_tasks, run_simulation  # noqa: E402

RUNS_DIR = db.ROOT / "evals" / "tau2_runs"
SEED = 300  # τ²'s default seed; trial i uses SEED + i

# The plan for the full runs (D22): the main configuration on every task, the ablations on the test split.
PLAN = [("single", "on", "base", 4), ("single", "off", "test", 4), ("routed", "on", "test", 4),
        ("routed", "off", "test", 4)]


# ---------- Claude as τ²'s customer and judge ----------
JUDGE_MODEL = ["claude-sonnet-5-5"]
PREFIX = ["anthropic"]  # litellm provider for the customer and the judge: "anthropic" (API) or "claude-sub" (D27)
_local = threading.local()  # judge usage of the current episode (each episode runs, and is judged, in one thread)
_tau2_generate = nl_module.generate


def _judge_generate(model: str = "", messages: Any = None, **kw: Any) -> Any:
    """τ²'s NL-assertion judge call, sent to Claude. Accepts a JSON answer wrapped in a code fence."""
    kw.pop("reasoning_effort", None)  # an OpenAI argument in τ²'s default judge settings
    msg = _tau2_generate(model=f"{PREFIX[0]}/{JUDGE_MODEL[0]}", messages=messages, **kw)
    text = (msg.content or "").strip()
    if text.startswith("```"):
        msg.content = text.strip("`").removeprefix("json").strip()
    if hasattr(_local, "judge"):
        _local.judge.append(msg.usage or {})
    return msg


def use_claude_judge(model: str) -> None:
    """τ²'s NL-assertion judge defaults to an OpenAI model (D22): point it at Claude."""
    JUDGE_MODEL[0] = model
    nl_module.generate = _judge_generate


def register_prices() -> None:
    """Teach litellm this repo's prices so τ²'s own cost fields aren't empty for these models."""
    litellm.register_model({f"{p}/{m}": {"input_cost_per_token": pin / 1e6, "output_cost_per_token": pout / 1e6,
                                         "litellm_provider": p, "mode": "chat"}
                            for m, (pin, pout) in PRICES.items() for p in ("anthropic", "claude-sub")})
    litellm.drop_params = True


def usd(model: str, usages: list[dict]) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return sum((u.get("prompt_tokens", 0) or 0) * pin + (u.get("completion_tokens", 0) or 0) * pout
               for u in usages) / 1e6


# ---------- one episode ----------
def agent_name(arch: str, gate: str) -> str:
    return f"order_ops_{arch}_gate_{gate}"


def register_agent(arch: str, gate: str, model: str, client: anthropic.Anthropic) -> str:
    name = agent_name(arch, gate)
    if name not in registry._agent_factories:
        def create(tools, domain_policy, **kw):  # noqa: ANN001
            task = kw.get("task")
            return tau2_adapter.P5Tau2Agent(tools, domain_policy, arch=arch, gate=gate, model=model, client=client,
                                            language_guidance=kw.get("language_guidance") or "",
                                            task_id=getattr(task, "id", None))
        registry.register_agent_factory(create, name)
    return name


def run_episode(config: TextRunConfig, task: Any, trial: int, user_model: str) -> dict[str, Any]:
    started = time.perf_counter()
    _local.judge = []
    orch = None
    try:
        orch = build_text_orchestrator(config, task, seed=SEED + trial)
        sim = run_simulation(orch, evaluation_type=EvaluationType.ALL)
        agent = orch.agent
        rep = agent.report()
        info = sim.reward_info
        user_usage = [m.usage for m in sim.messages if isinstance(m, UserMessage) and m.usage]
        user_cost, judge_cost = usd(user_model, user_usage), usd(JUDGE_MODEL[0], _local.judge)
        nl = [{"assertion": c.nl_assertion, "met": c.met} for c in (info.nl_assertions or [])]
        return {
            "task_id": task.id, "trial": trial, "reward": float(info.reward or 0.0),
            "db_match": bool(info.db_check.db_match) if info.db_check else None,
            "nl_ok": all(c["met"] for c in nl) if nl else None, "nl": nl,
            "termination": str(sim.termination_reason), "turns": sum(isinstance(m, UserMessage) for m in sim.messages),
            "arch": rep["arch"], "gate": rep["gate"], "gate_stats": rep["gate_stats"],
            "tool_calls": [{"name": c["name"], "args": c["args"], "ran_in_tau2": c["ran_in_tau2"],
                            "blocked": c["result"].get("blocked_by_policy"), "ok": c["result"].get("ok")}
                           for c in rep["tool_calls"]],
            "routes": rep["routes"], "handoffs": rep["handoffs"], "agent_model": rep["model"],
            "usage": {"agent": rep["usage"], "user_calls": len(user_usage), "judge_calls": len(_local.judge),
                      "user_tokens_in": sum(u.get("prompt_tokens", 0) or 0 for u in user_usage),
                      "user_tokens_out": sum(u.get("completion_tokens", 0) or 0 for u in user_usage)},
            "agent_cost_usd": rep["agent_cost_usd"], "user_cost_usd": round(user_cost, 5),
            "judge_cost_usd": round(judge_cost, 5),
            "cost_usd": round(rep["agent_cost_usd"] + user_cost + judge_cost, 5),
            "trace_id": rep["trace_id"], "wall_s": round(time.perf_counter() - started, 1),
            "transcript": [f"{'user' if isinstance(m, UserMessage) else 'assistant'}: {m.content}"
                           for m in sim.messages if getattr(m, "content", None)
                           and type(m).__name__ in ("UserMessage", "AssistantMessage")],
        }
    except Exception as e:  # noqa: BLE001 - one broken episode must not kill the run
        return {"task_id": task.id, "trial": trial, "reward": 0.0, "error": f"{type(e).__name__}: {e}",
                "wall_s": round(time.perf_counter() - started, 1)}
    finally:
        if orch is not None:
            orch.agent.close()


# ---------- summary ----------
def summarize(results: list[dict], trials: int) -> dict[str, Any]:
    ok = [r for r in results if "error" not in r]
    n = max(len(ok), 1)

    def total(key: str) -> dict:
        out: dict[str, int] = {}
        for r in ok:
            for k, v in r["gate_stats"].get(key, {}).items():
                out[k] = out.get(k, 0) + v
        return out

    # shadow mode (gate off): the calls the gate would have stopped, split by whether the episode passed
    would_fail = sum(sum(r["gate_stats"].get("would_block", {}).values()) for r in ok if not r["reward"])
    would_pass = sum(sum(r["gate_stats"].get("would_block", {}).values()) for r in ok if r["reward"])
    per_ep = sum(r["cost_usd"] for r in ok) / n
    return {
        **{f"pass^{k}": round(pass_hat_k(results, k), 3) for k in range(1, trials + 1)},
        "episodes": len(results), "errors": len(results) - len(ok),
        "db_match_rate": round(sum(bool(r["db_match"]) for r in ok) / n, 3),
        "nl_ok_rate": round(sum(r["nl_ok"] is not False for r in ok) / n, 3),
        "blocked_by_gate": total("blocked"), "would_block_if_on": total("would_block"),
        "would_block_in_failed_episodes": would_fail, "would_block_in_passed_episodes": would_pass,
        "approvals_needed": sum(r["gate_stats"]["approvals"] for r in ok),
        "terminations": _count(r["termination"] for r in ok),
        "avg_turns": round(sum(r["turns"] for r in ok) / n, 1),
        "avg_agent_calls": round(sum(r["usage"]["agent"]["calls"] for r in ok) / n, 1),
        "cost_per_episode_usd": {"agent": round(sum(r["agent_cost_usd"] for r in ok) / n, 4),
                                 "user": round(sum(r["user_cost_usd"] for r in ok) / n, 4),
                                 "judge": round(sum(r["judge_cost_usd"] for r in ok) / n, 4),
                                 "total": round(per_ep, 4)},
        "total_cost_usd": round(sum(r["cost_usd"] for r in ok), 3),
    }


def _count(items) -> dict:  # noqa: ANN001
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


def plan_cost(per_episode: float, routed_factor: float = 1.15) -> list[dict]:
    """What the full plan would cost at the measured cost per episode (routed adds a router call per turn)."""
    sizes = {s: len(json.loads((db.TAU2_DIR / "split_tasks.json").read_text())[s]) for s in ("base", "test")}
    rows = []
    for arch, gate, split, trials in PLAN:
        eps = sizes[split] * trials
        cost = eps * per_episode * (routed_factor if arch == "routed" else 1.0)
        rows.append({"arch": arch, "gate": gate, "split": split, "trials": trials, "episodes": eps,
                     "est_usd": round(cost, 2)})
    return rows


# ---------- main ----------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="test", choices=["train", "test", "base"])
    ap.add_argument("--tasks", help="comma-separated task ids (overrides --split)")
    ap.add_argument("--sample", type=int, help="run only N tasks of the split (seeded pick), to measure cost first")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--arch", default="single", choices=["single", "routed"])
    ap.add_argument("--gate", default="on", choices=["on", "off"])
    ap.add_argument("--agent-model", default=os.getenv("AGENT_MODEL", "claude-haiku-5-5"))
    ap.add_argument("--user-model", default=os.getenv("TAU2_USER_MODEL", "claude-haiku-5-5"))
    ap.add_argument("--user-args", default='{"temperature": 0.0}', help="JSON; τ²'s default customer args")
    ap.add_argument("--judge-model", default=os.getenv("JUDGE_MODEL", "claude-sonnet-5-5"))
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-steps", type=int, default=200, help="τ²'s default")
    ap.add_argument("--label", default=time.strftime("tau2_%Y%m%d_%H%M"))
    ap.add_argument("--backend", default=os.getenv("LLM_BACKEND", "api"), choices=["api", "subscription"],
                    help="subscription: agent, customer and judge on the Claude plan via the Agent SDK (D27)")
    args = ap.parse_args()

    if args.backend == "api" and not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set (add it to .env), or use --backend subscription.")
    client = make_client(args.backend)
    if args.backend == "subscription":
        from .subscription import PROVIDER, register_litellm
        register_litellm()
        PREFIX[0] = PROVIDER
    register_prices()
    use_claude_judge(args.judge_model)
    tracing.setup()
    name = register_agent(args.arch, args.gate, args.agent_model, client)
    config = TextRunConfig(domain="retail", agent=name, llm_agent=args.agent_model,
                           llm_user=f"{PREFIX[0]}/{args.user_model}", llm_args_user=json.loads(args.user_args),
                           max_steps=args.max_steps, seed=SEED)

    if args.tasks:
        ids = args.tasks.split(",")
    else:
        ids = json.loads((db.TAU2_DIR / "split_tasks.json").read_text())[args.split]
        if args.sample:
            ids = sorted(random.Random(0).sample(ids, args.sample), key=int)
    tasks = get_tasks("retail", task_ids=ids)
    jobs = [(t, i) for t in tasks for i in range(args.trials)]
    out_dir = RUNS_DIR / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} episodes · agent {args.agent_model} ({args.arch}, gate {args.gate}) · customer "
          f"{args.user_model} · judge {args.judge_model} → {out_dir}", flush=True)

    run_config = {"tasks": ",".join(ids), "arch": args.arch, "gate": args.gate,
                  "agent_model": args.agent_model, "user_model": args.user_model, "user_args": args.user_args,
                  "judge_model": args.judge_model, "max_steps": args.max_steps, "backend": args.backend}
    runlog.pin_config(out_dir, run_config)

    def describe(r: dict) -> str:
        mark = "ERR " + r["error"][:120] if "error" in r else ("PASS" if r["reward"] else "fail")
        return f"task {r['task_id']} t{r['trial']} {mark}"

    results, stopped = runlog.run_jobs(jobs, lambda j: (str(j[0].id), j[1]),
                                       lambda j: run_episode(config, j[0], j[1], args.user_model),
                                       out_dir, args.concurrency, describe)

    summary = summarize(results, args.trials) | {
        "label": args.label, "tasks": len(tasks), "trials": args.trials, "arch": args.arch, "gate": args.gate,
        "agent_model": args.agent_model, "user_model": args.user_model, "judge_model": args.judge_model,
        "tau2_commit": "989d3285ae669e49503324bdb9981bf14fccedeb", "configuration": "custom (D22)",
        "backend": args.backend, "stopped": stopped, "planned_episodes": len(jobs),
        "complete": stopped is None and len(results) == len(jobs) and not any("error" in r for r in results),
    }
    if args.backend == "subscription":
        from .subscription import usage_now
        summary["plan_usage_at_end"] = usage_now()
    if args.sample and summary["episodes"] > summary["errors"]:
        summary["full_plan_estimate"] = plan_cost(summary["cost_per_episode_usd"]["total"])
        summary["full_plan_estimate_total_usd"] = round(sum(r["est_usd"] for r in summary["full_plan_estimate"]), 2)
    runlog.write_json(out_dir / "summary.json", summary)
    tracing.flush(10000)
    print(json.dumps(summary, indent=2))
    runlog.finish(stopped, results, len(jobs), "order_ops.tau2_run", args.label)


if __name__ == "__main__":
    main()
