"""Agreement between human labels and the automatic grader / failure reviewer on final_own (D35).

Usage: uv run python evals/human_label_agreement.py
"""

import json
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent / "human_labels/final_own.json"
AGENT_FAULTS = {"agent_wrong_change", "agent_process_error"}


def kappa(a: list[bool], b: list[bool]) -> tuple[float, float]:
    n = len(a)
    p_o = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    p_e = pa * pb + (1 - pa) * (1 - pb)
    return p_o, (p_o - p_e) / (1 - p_e) if p_e < 1 else 1.0


def main():
    rows = json.loads(DATA.read_text())["rows"]
    n = len(rows)
    grader_pass = [r["grader"]["pass"] for r in rows]
    print(f"{n} episodes · grader: {sum(grader_pass)} pass / {n - sum(grader_pass)} fail\n")

    for stage in ("blind", "final"):
        human_ok = [r[stage]["outcome"] == "correct" for r in rows]
        p_o, k = kappa(human_ok, grader_pass)
        print(f"[{stage}] human correct {sum(human_ok)}/{n} · agreement with grader {p_o:.1%} · Cohen's kappa {k:.2f}")

    fails = [r for r in rows if not r["grader"]["pass"]]
    passes = [r for r in rows if r["grader"]["pass"]]
    why = Counter(r["final"]["fault"] or "customer_driven (agent correct)" for r in fails)
    print(f"\nGrader failures ({len(fails)}), human reason:")
    for k, v in why.most_common():
        print(f"  {v:2d}  {k}")
    agent_err = [r for r in rows if r["final"]["fault"] in AGENT_FAULTS]
    missed = [r for r in agent_err if r["grader"]["pass"]]
    print(f"\nAgent errors found by the human: {len(agent_err)} · also failed by the grader: {len(agent_err) - len(missed)}")
    print(f"Grader failures that are agent errors: {len(agent_err) - len(missed)}/{len(fails)}")
    flagged = [r for r in passes if r["final"]["outcome"] != "correct" or r["final"]["fault"]]
    print(f"Grader passes the human disagreed with or flagged: {len(flagged)}/{len(passes)} "
          f"({', '.join(f'item {r['item']}: {r['final']['fault']}' for r in flagged) or 'none'})")

    rev = [r for r in rows if r["reviewer_verdict"]]
    def human_type(r):
        f = r["final"]["fault"]
        return f if f in AGENT_FAULTS else "customer_driven"
    same = [r for r in rev if human_type(r) == r["reviewer_verdict"]]
    print(f"\nFailure reviewer vs human on the {len(rev)} episodes it triaged: {len(same)}/{len(rev)} same type")
    for r in rev:
        if r not in same:
            print(f"  item {r['item']} (task {r['task_id']} t{r['trial']}): reviewer {r['reviewer_verdict']}, "
                  f"human {human_type(r)}")


if __name__ == "__main__":
    main()
