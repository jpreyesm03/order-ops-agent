# τ²-bench retail domain (pinned copy)

- Source: https://github.com/sierra-research/tau2-bench, tag `v2.0.0` (commit `989d328`), path `data/tau2/domains/retail/`
- License: MIT (see `LICENSE` in this folder), Copyright (c) 2025 Sierra Research
- Files: `db.json` (500 users, 1,000 orders, 50 products), `tasks.json` (114 tasks), `split_tasks.json` (train 74 / test 40), `policy.md` (agent policy)

Only the data and the policy are used. Tools, agent, user simulator and evaluator in this repo are my own implementation.

## English language pack (added 2026-11-11)
- Source: same commit, path `data/tau2/multilingual/en/` (retail files and `pack.yaml` only).
- τ² 2.0's standard English text run replaces the customer scenario of 50 retail tasks with reviewed wording
  (adds name details to what the customer knows) and gives the simulated customer a persona. Grading criteria are
  identical to `tasks.json` for all 114 tasks (checked). Used by `order_ops.tau2_adapter` (τ²'s own harness) and
  overlaid in `order_ops.evaluate` so both harnesses give the customer the same instructions.
