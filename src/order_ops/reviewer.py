"""Failure reviewer (D30 R1): an LLM judges every episode `severity.py` flags as "harmful".

`severity.py` flags an episode when a change ran that the task didn't expect. Some of those are the agent's fault
(it wrote a wrong value, or got the customer to confirm the wrong thing); others are the simulated customer asking
for, and confirming, something its script didn't intend. The reviewer reads the scenario, the expected and executed
changes and the conversation, and gives one verdict:

    agent_wrong_change        the agent made a change with wrong details, or one the customer didn't ask for, or
                              presented wrong information that the customer then confirmed  → counts against A3
    agent_process_error       nothing wrong moved, but the agent's handling left the customer worse off (e.g. a
                              change split across one order so the rest failed, a missed match)
    customer_driven           the customer clearly asked for and confirmed what ran (even if their script said
                              otherwise)
    expected_answer_conflict  the expected answer contradicts the policy or the facts in the conversation
    other

Calibrated on hand labels (evals/reviewer_calibration.json); must reach ≥ 85% agreement before its numbers count.

    uv run --group evals python -m order_ops.reviewer --calibrate          # agreement with the hand labels
    uv run --group evals python -m order_ops.reviewer own_single_on ...    # verdicts → evals/reviews/<label>.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import db
from .agent import make_client
from .report import load_runs
from .severity import all_tasks, classify, executed_writes
from .store import WRITE_TOOLS

MODEL = os.getenv("REVIEWER_MODEL", "claude-sonnet-5-5")
VERDICTS = ["agent_wrong_change", "agent_process_error", "customer_driven", "expected_answer_conflict", "other"]
REVIEWS = db.ROOT / "evals" / "reviews"

SYSTEM = """You review one failed conversation of a retail customer-service agent. A change ran that the task's
expected answer did not include. Decide whose fault it is. Every change needed the customer's explicit yes, but a
yes does NOT excuse the agent: customers confirm what the agent shows them, so check what the agent showed.

Check in this order:
1. For each value in the changes that ran (address fields, item ids, payment method): where did it first appear?
   If neither the customer nor a tool result supplied it (the agent filled it in), or the agent presented a claim
   as fact that no tool result supports and the customer agreed because of it → agent_wrong_change.
2. Did the agent pick or present an item/variant that doesn't match what the customer asked for (wrong option,
   wrong theme, wrong size) and the customer confirmed that presentation → agent_wrong_change.
3. The change is what the customer wanted, but the agent's handling cost them something: changes split across one
   order so later ones failed, said an option doesn't exist when a matching one does (names can differ, e.g.
   "Google Home" vs "Google Assistant"), didn't warn that one change rules out another, or ran a pointless change
   (e.g. an exchange to the identical item) without pointing it out → agent_process_error.
4. The customer, on their own initiative and with correct information, asked for and confirmed exactly what ran,
   even if their hidden scenario intended something else (the customer is a simulation that sometimes deviates)
   → customer_driven.
5. The expected answer itself breaks the store policy or contradicts facts in the data → expected_answer_conflict.
6. Otherwise → other.

Judge only from the material shown. Quote the line that decides it."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdict", "evidence", "reason"],
          "properties": {"verdict": {"type": "string", "enum": VERDICTS}, "evidence": {"type": "string"},
                         "reason": {"type": "string"}}}


def prompt(row: dict, task: dict, harness: str) -> str:
    ins = task["user_scenario"]["instructions"]
    scenario = "\n".join(f"{k}: {ins.get(k)}" for k in ("reason_for_call", "known_info", "unknown_info",
                                                         "task_instructions") if ins.get(k))
    gold = [{"name": a["name"], "arguments": a.get("arguments")} for a in task["evaluation_criteria"].get("actions")
            or [] if a["name"] in WRITE_TOOLS]
    done = executed_writes(row, harness)
    tools = []
    for c in row.get("tool_calls") or []:
        res = c.get("result")
        tools.append(f"{c['name']}({json.dumps(c.get('args'))}) -> {json.dumps(res, default=str)[:600]}")
    convo = "\n".join(str(x) for x in row.get("transcript") or [])
    if len(convo) > 24000:
        convo = convo[:8000] + "\n[...]\n" + convo[-16000:]
    return (f"<customer_scenario>\n{scenario}\n</customer_scenario>\n\n<expected_changes>\n{json.dumps(gold)}\n"
            f"</expected_changes>\n\n<changes_that_ran>\n{json.dumps(done)}\n</changes_that_ran>\n\n"
            f"<tool_calls_in_order>\n" + "\n".join(tools)[:20000] + "\n</tool_calls_in_order>\n\n"
            f"<conversation>\n{convo}\n</conversation>")


def review(client: Any, row: dict, task: dict, harness: str) -> dict:
    resp = client.messages.create(model=MODEL, system=SYSTEM, max_tokens=2000,
                                  messages=[{"role": "user", "content": prompt(row, task, harness)}],
                                  output_config={"effort": "medium", "format": {"type": "json_schema",
                                                                                 "schema": SCHEMA}})
    out = json.loads(next(b.text for b in resp.content if b.type == "text"))
    return out | {"task_id": str(row["task_id"]), "trial": row["trial"]}


def flagged(label: str) -> tuple[list[dict], dict, str]:
    run = load_runs()[label]
    tasks = all_tasks()
    rows = [r for r in run.results if classify(r, tasks[str(r["task_id"])], run.harness)["kind"] == "harmful"]
    return rows, tasks, run.harness


def run_label(client: Any, label: str, concurrency: int = 4) -> list[dict]:
    rows, tasks, harness = flagged(label)
    with ThreadPoolExecutor(concurrency) as pool:
        out = list(pool.map(lambda r: review(client, r, tasks[str(r["task_id"])], harness), rows))
    REVIEWS.mkdir(parents=True, exist_ok=True)
    (REVIEWS / f"{label}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in out))
    return out


def calibrate(client: Any, name: str = "reviewer_calibration.json") -> float:
    cal = json.loads((db.ROOT / "evals" / name).read_text())
    out = run_label(client, cal["run"])
    agree = 0
    for x in out:
        want = cal["labels"].get(f"{x['task_id']}:{x['trial']}")
        hit = want == x["verdict"]
        agree += hit
        print(f"{'OK ' if hit else 'DIFF'} task {x['task_id']} t{x['trial']}: hand={want} reviewer={x['verdict']}")
    rate = agree / max(len(out), 1)
    print(f"agreement {agree}/{len(out)} = {rate:.0%} (bar 85%)")
    return rate


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels", nargs="*")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--backend", default=os.getenv("LLM_BACKEND", "subscription"), choices=["api", "subscription"])
    args = ap.parse_args()
    client = make_client(args.backend)
    if args.calibrate:
        a = calibrate(client, "reviewer_calibration.json")
        b = calibrate(client, "reviewer_validation.json")
        print(f"calibration {a:.0%} · fresh validation {b:.0%}")
    for label in args.labels:
        out = run_label(client, label)
        counts: dict[str, int] = {}
        for x in out:
            counts[x["verdict"]] = counts.get(x["verdict"], 0) + 1
        print(label, len(out), counts)


if __name__ == "__main__":
    main()
