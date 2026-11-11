# τ²-bench runs: runbook

This agent on τ²-bench retail, inside τ²'s own orchestrator, user simulator and evaluator (D21–D23).
Custom configuration, not a leaderboard submission (D22): the agent is this repo's (prompt, tools, loop, router,
policy gate). Both the simulated customer and the judge are Claude models.

## Setup (once)
```bash
uv sync --group tau2            # τ² pinned to commit 989d328 (v2.0.0) in uv.lock; litellm locked at 1.82.6
# .env: ANTHROPIC_API_KEY=...   (set a spend cap on the key in the Anthropic console first)
```

## 1. Sample first (5 test tasks, 1 trial, about 10 cents)
```bash
uv run --group tau2 python -m order_ops.tau2_run --split test --sample 5 --label tau2_sample
```
It prints `[n/total] … $ so far · ETA` per episode. `evals/tau2_runs/tau2_sample/summary.json` then has
`cost_per_episode_usd` (agent / customer / judge) and `full_plan_estimate`: what the plan below would cost
at the measured rate.

Things to check before going further:
- `errors` is 0. If the customer model rejects `temperature`, rerun with `--user-args '{}'`.
- A few transcripts in `results.jsonl` read like real conversations: tool calls with `ran_in_tau2`, gate blocks.

## 2. The plan (one configuration at a time; check each summary before the next)
| # | Command | Episodes |
|---|---|---|
| 1 | `uv run --group tau2 python -m order_ops.tau2_run --split base --trials 4 --arch single --gate on --label tau2_single_on` | 114 × 4 = 456 |
| 2 | `uv run --group tau2 python -m order_ops.tau2_run --split test --trials 4 --arch single --gate off --label tau2_single_off` | 40 × 4 = 160 |
| 3 | `uv run --group tau2 python -m order_ops.tau2_run --split test --trials 4 --arch routed --gate on --label tau2_routed_on` | 160 |
| 4 | `uv run --group tau2 python -m order_ops.tau2_run --split test --trials 4 --arch routed --gate off --label tau2_routed_off` | 160 |

Plus the same agent on this repo's own harness, same 40 test tasks. This is the harness comparison (D22):
```bash
uv run python -m order_ops.evaluate --split test --trials 4 --label own_single_on
```

## Cost estimate (before measuring)
Built from the real prompt size (system prompt + policy + tool definitions ≈ 4.6k tokens, cached). Assumptions
(stated, not measured): 10–22 agent calls, 5–11 customer messages and 1 judge call per episode.

| Part | Model | Per episode |
|---|---|---|
| Agent | Haiku 5.5, prompt caching | $0.002–0.007 |
| Simulated customer | Haiku 5.5 (litellm, no caching) | $0.001–0.003 |
| NL-assertion judge | Sonnet 5.5 | ~$0.015 |
| **Total** | | **~$0.017–0.025** |

The full plan is 936 episodes, about **$17–25**. The judge is ~75% of the cost, so a cheaper judge is the lever
if the budget is tight; it would also be a less reliable grader. For comparison, a gpt-4.1 customer would cost
about $0.024 per episode on its own, about 8× the agent.

## What gets reported
- pass^1…pass^4 per configuration, DB-match rate, NL-assertion rate, cost per episode, termination reasons.
- Gate on: `blocked_by_gate` by rule. Gate off (shadow mode): `would_block_if_on`, split into calls in failed
  and passed episodes.
- Single vs routed (D11) on the same harness and customer.
- This harness vs τ²'s harness on the same 40 tasks.
- Context only, never a ranking: Sierra's published 1.x retail numbers (Sonnet 4.5 72.4 / 39.5 pass^1 / pass^4,
  gpt-5.2 customer). Different version, customer and model.
