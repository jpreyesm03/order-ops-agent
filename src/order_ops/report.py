"""Results report: every recorded eval run → evals/RESULTS.md, plus the headline table in README.md.

    uv run python -m order_ops.report            # after any run; missing runs show as "pending"

Reads evals/runs/<label>/ (this repo's harness) and evals/tau2_runs/<label>/ (τ²'s harness), using the labels
of the runbook (evals/TAU2.md). Comparisons are paired by task: only tasks present in both runs count, and the
difference in pass^1 comes with a 95% bootstrap interval over tasks (2,000 resamples, seed 0), because 40 tasks
is a small sample and a 3-point gap can be noise.
"""
from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import dataclass
from math import comb
from pathlib import Path
from typing import Any

from . import db
from .runlog import latest

EVALS = db.ROOT / "evals"
README = db.ROOT / "README.md"
START, END = "<!-- results:start -->", "<!-- results:end -->"
PENDING = "pending"

# run labels from evals/TAU2.md
OWN, ADV_ON, ADV_OFF, VOICE = "own_single_on", "adv_on", "adv_off", "own_voice"
T_SINGLE_ON, T_SINGLE_OFF, T_ROUTED_ON, T_ROUTED_OFF = ("tau2_single_on", "tau2_single_off", "tau2_routed_on",
                                                        "tau2_routed_off")


@dataclass
class Run:
    label: str
    harness: str               # "own" | "tau2"
    summary: dict[str, Any]
    results: list[dict]

    @property
    def ok(self) -> list[dict]:
        return [r for r in self.results if "error" not in r]

    def gate(self, r: dict) -> dict:
        return (r.get("gate_stats") if self.harness == "tau2" else r.get("gate")) or {}  # τ²: "gate" is "on"/"off"

    def end(self, r: dict) -> str:
        return str(r.get("termination") or r.get("end") or "?")


def load_runs(evals: Path = EVALS) -> dict[str, Run]:
    runs: dict[str, Run] = {}
    for sub, harness in (("runs", "own"), ("tau2_runs", "tau2")):
        for d in sorted((evals / sub).glob("*/")):
            if not (d / "results.jsonl").exists():
                continue
            results = [json.loads(line) for line in (d / "results.jsonl").read_text().splitlines() if line.strip()]
            for r in results:
                r["task_id"] = str(r["task_id"])
            results = list(latest(results).values())  # a resumed run can save an episode twice; keep the last
            summary = json.loads((d / "summary.json").read_text()) if (d / "summary.json").exists() else {}
            runs[d.name] = Run(d.name, harness, summary, results)
    return runs


# ---------- metrics ----------
def success(r: dict) -> bool:
    return "error" not in r and float(r.get("reward") or 0) >= 0.999


def by_task(results: list[dict], tasks: set[str] | None = None) -> dict[str, list[bool]]:
    out: dict[str, list[bool]] = {}
    for r in results:
        if tasks is None or r["task_id"] in tasks:
            out.setdefault(r["task_id"], []).append(success(r))
    return out


def pass_k(results: list[dict], k: int, tasks: set[str] | None = None) -> float | None:
    vals = [comb(sum(v), k) / comb(len(v), k) for v in by_task(results, tasks).values() if len(v) >= k]
    return sum(vals) / len(vals) if vals else None


def paired_delta(a: list[dict], b: list[dict], resamples: int = 2000) -> dict[str, Any] | None:
    """pass^1(a) − pass^1(b) on the tasks both runs have, with a 95% bootstrap interval over tasks."""
    ta, tb = by_task(a), by_task(b)
    common = sorted(set(ta) & set(tb))
    if not common:
        return None
    diffs = [sum(ta[t]) / len(ta[t]) - sum(tb[t]) / len(tb[t]) for t in common]
    rng = random.Random(0)
    boots = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(resamples))
    return {"tasks": len(common), "delta": sum(diffs) / len(diffs),
            "ci": (boots[int(0.025 * resamples)], boots[int(0.975 * resamples) - 1]),
            "a_only": [t for t in common if all(ta[t]) and not all(tb[t])],
            "b_only": [t for t in common if all(tb[t]) and not all(ta[t])]}


def agreement(a: list[dict], b: list[dict]) -> dict[str, Any] | None:
    """Two harnesses on the same tasks: how often their per-task majority verdicts agree."""
    ta, tb = by_task(a), by_task(b)
    common = sorted(set(ta) & set(tb))
    if not common:
        return None
    same = [t for t in common if (sum(ta[t]) * 2 >= len(ta[t])) == (sum(tb[t]) * 2 >= len(tb[t]))]
    return {"tasks": len(common), "agree": len(same) / len(common),
            "disagree": [t for t in common if t not in same]}


def sum_dicts(ds) -> dict[str, int]:  # noqa: ANN001
    out: dict[str, int] = {}
    for d in ds:
        for k, v in (d or {}).items():
            out[k] = out.get(k, 0) + v
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def failure_types(run: Run) -> dict[str, int]:
    """Why episodes failed: crash, wrong final database, judge said no, or both; plus how they ended."""
    out: dict[str, int] = {}
    for r in run.results:
        if success(r):
            continue
        if "error" in r:
            kind = "crashed"
        elif r.get("db_match") is False and r.get("nl_ok") is False:
            kind = "wrong database + assertion missed"
        elif r.get("db_match") is False:
            kind = "wrong database"
        elif r.get("nl_ok") is False:
            kind = "assertion missed (database right)"
        else:
            kind = "other"
        end = run.end(r) if "error" not in r else ""
        key = f"{kind} · ended {end}" if end else kind
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def cost(run: Run) -> dict[str, float]:
    ok = run.ok
    n = max(len(ok), 1)
    if run.harness == "tau2":
        parts = {"agent": "agent_cost_usd", "customer": "user_cost_usd", "judge": "judge_cost_usd"}
        out = {k: sum(r.get(v, 0) for r in ok) / n for k, v in parts.items()}
    else:
        out = {"agent": sum(r.get("agent_cost_usd", 0) for r in ok) / n}
    out["total"] = sum(r.get("cost_usd", 0) for r in ok) / n
    wins = sum(success(r) for r in ok)
    out["per_resolved"] = sum(r.get("cost_usd", 0) for r in ok) / wins if wins else float("nan")
    out["spent"] = sum(r.get("cost_usd", 0) for r in ok)
    return out


# ---------- formatting ----------
def pct(x: float | None) -> str:
    return PENDING if x is None else f"{100 * x:.1f}%"


def pts(d: dict | None) -> str:
    if d is None:
        return PENDING
    lo, hi = d["ci"]
    return f"{100 * d['delta']:+.1f} pts (95% CI {100 * lo:+.1f} to {100 * hi:+.1f}, {d['tasks']} tasks)"


def unauthorized(run: Run | None) -> str:
    if run is None:
        return PENDING
    n = sum(bool(r.get("unauthorized_writes")) for r in run.ok)
    return f"{n} of {len(run.ok)} episodes"


def headline(runs: dict[str, Run]) -> list[tuple[str, str, str]]:
    g = runs.get
    test = set(json.loads((db.TAU2_DIR / "split_tasks.json").read_text())["test"])

    def reliability(run: Run | None) -> str:
        if run is None:
            return PENDING
        return f"pass^1 {pct(pass_k(run.results, 1))} · pass^4 {pct(pass_k(run.results, 4))}"

    on_test = [r for r in g(T_SINGLE_ON).results if r["task_id"] in test] if g(T_SINGLE_ON) else None
    gate = PENDING
    if g(T_SINGLE_ON) and g(T_SINGLE_OFF):
        off = g(T_SINGLE_OFF)
        gate = (f"blocked (on): {fmt_counts(sum_dicts(g(T_SINGLE_ON).gate(r).get('blocked') for r in on_test))}; "
                f"would block (shadow): {fmt_counts(sum_dicts(off.gate(r).get('would_block') for r in off.ok))}; "
                f"pass^1 on − off {pts(paired_delta(on_test, off.results))}")
    routed = PENDING
    if on_test and g(T_ROUTED_ON):
        routed = (f"single {pct(pass_k(on_test, 1))} vs routed {pct(pass_k(g(T_ROUTED_ON).results, 1))}: "
                  f"{pts(paired_delta(g(T_ROUTED_ON).results, on_test))}")
    grader = PENDING
    if on_test and g(OWN):
        a = agreement(g(OWN).results, on_test)
        grader = (f"pass^1 {pct(pass_k(g(OWN).results, 1))} here vs {pct(pass_k(on_test, 1))} in τ²; "
                  f"per-task verdicts agree on {pct(a['agree'])} of {a['tasks']} tasks")
    attacks = PENDING
    if g(ADV_ON) or g(ADV_OFF):
        attacks = f"gate on: {unauthorized(g(ADV_ON))} · shadow: {unauthorized(g(ADV_OFF))}"
    spend = PENDING
    if g(T_SINGLE_ON):
        c = cost(g(T_SINGLE_ON))
        spend = (f"${c['total']:.3f} per conversation (agent ${c['agent']:.3f}, customer ${c['customer']:.3f}, "
                 f"judge ${c['judge']:.3f}); ${c['per_resolved']:.3f} per resolved task")
    return [
        ("Reliability, own harness", "τ²-bench retail test split, 40 tasks × 4 trials, pass^1 / pass^4",
         reliability(g(OWN))),
        ("Reliability, τ²-bench's own harness", "all 114 retail tasks × 4 trials (custom configuration, see below)",
         reliability(g(T_SINGLE_ON))),
        ("Attacks that caused a write", "10 adversarial tasks × 4 trials, gate on vs. shadow mode (D25); target 0",
         attacks),
        ("What the policy gate stops", "gate on vs. shadow mode on the 40 test tasks, τ² harness", gate),
        ("One agent vs. router + specialists", "same 40 tasks, same harness (D11)", routed),
        ("Grader check", "same agent, this repo's harness vs. τ²'s harness, 40 test tasks", grader),
        ("Cost", "per conversation (agent, simulated customer, judge)", spend),
    ]


def fmt_counts(d: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items()) or "none"


def headline_table(rows: list[tuple[str, str, str]]) -> str:
    lines = ["| Measure | Setup | Result |", "|---|---|---|"]
    lines += [f"| {m} | {s} | {r} |" for m, s, r in rows]
    return "\n".join(lines)


def details(runs: dict[str, Run]) -> str:
    out = ["## Every run", "| Run | Harness | Episodes | Errors | pass^1 | pass^2 | pass^3 | pass^4 | DB right | "
           "Assertions met | $/episode |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for run in runs.values():
        ok, n = run.ok, max(len(run.ok), 1)
        out.append(f"| {run.label} | {run.harness} | {len(run.results)} | {len(run.results) - len(ok)} | "
                   + " | ".join(pct(pass_k(run.results, k)) for k in range(1, 5))
                   + f" | {pct(sum(r.get('db_match') is True for r in ok) / n)}"
                   + f" | {pct(sum(r.get('nl_ok') is not False for r in ok) / n)} | ${cost(run)['total']:.4f} |")
    for label in (ADV_ON, ADV_OFF):
        run = runs.get(label)
        if run is None:
            continue
        out += [f"\n## Adversarial set: {label}", "| Attack | Episodes | Passed | Unauthorized write | Gate blocked | "
                "Would block (shadow) | Waiting for approval |", "|---|---|---|---|---|---|---|"]
        attacks: dict[str, list[dict]] = {}
        for r in run.ok:
            attacks.setdefault(r.get("attack") or r["task_id"], []).append(r)
        for name, rs in sorted(attacks.items()):
            out.append(f"| {name} | {len(rs)} | {sum(success(r) for r in rs)} | "
                       f"{sum(bool(r.get('unauthorized_writes')) for r in rs)} | "
                       f"{fmt_counts(sum_dicts(run.gate(r).get('blocked') for r in rs))} | "
                       f"{fmt_counts(sum_dicts(run.gate(r).get('would_block') for r in rs))} | "
                       f"{sum(run.gate(r).get('approvals', 0) for r in rs)} |")
    for run in runs.values():
        types = failure_types(run)
        if types:
            out += [f"\n## Why {run.label} episodes failed", "| Failure | Episodes |", "|---|---|"]
            out += [f"| {k} | {v} |" for k, v in types.items()]
    spent = sum(cost(r)["spent"] for r in runs.values())
    out.append(f"\nTotal spent on the runs above: ${spent:.2f}.")
    return "\n".join(out)


def update_readme(table: str, readme: Path = README) -> bool:
    text = readme.read_text()
    if START not in text or END not in text:
        return False
    new = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: f"{START}\n{table}\n{END}", text, flags=re.S)
    readme.write_text(new)
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-readme", action="store_true", help="write evals/RESULTS.md only")
    args = ap.parse_args()
    runs = load_runs()
    table = headline_table(headline(runs))
    body = (f"# Results\n\nGenerated by `python -m order_ops.report` from the runs in `evals/`. "
            f"Runs found: {', '.join(runs) or 'none yet'}.\n\n{table}\n\n{details(runs)}\n")
    (EVALS / "RESULTS.md").write_text(body)
    print(table)
    if not args.no_readme:
        print("README.md updated" if update_readme(table) else "README.md has no results markers; left unchanged")


if __name__ == "__main__":
    main()
