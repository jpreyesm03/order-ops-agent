# Order Ops Agent

A customer-service agent that cancels, refunds, exchanges and edits orders for an online store, under a written
policy, with the risky parts enforced in code instead of trusted to the model.

**Live demo:** https://order-ops-agent.vercel.app (a real Shopify dev store; the supervisor console approves refunds
over $250) · **Decisions:** [DECISIONS.md](DECISIONS.md) · **Client brief:** [CLIENT_BRIEF.md](CLIENT_BRIEF.md)

## Results
> Pending: the eval runs below have not been run yet. Every number here will come from a recorded run in `evals/`.

| Measure | Setup | Result |
|---|---|---|
| Reliability, own harness | τ²-bench retail test split, 40 tasks × 4 trials, pass^1 / pass^4 | pending |
| Reliability, τ²-bench's own harness | all 114 retail tasks × 4 trials (custom configuration, see below) | pending |
| What the policy gate stops | gate on vs. shadow mode: blocked calls by rule, in failed vs. passed episodes | pending |
| Attacks that caused a write | 10 adversarial tasks × 4 trials, gate on vs. shadow mode (D25); target 0 | pending |
| One agent vs. router + specialists | same 40 tasks, same harness (D11) | pending |
| Grader check | same agent, this repo's harness vs. τ²'s harness | pending |
| Cost | per conversation (agent, simulated customer, judge) | pending |

τ²-bench numbers are a **custom configuration**, not a leaderboard submission: the agent has a router and a policy
gate, and both the simulated customer and the judge are Claude models (D22). Published scores use other models and
customer simulators and are not directly comparable.

## How it works
```
customer ─▶ Next.js chat ─▶ FastAPI on AWS Lambda ─▶ agent loop (Claude Haiku, tool use)
                                                        │ every tool call
                                                        ▼
                                                   policy gate (code, not prompt)
                                identity · one customer · ownership · explicit "yes" per change
                                approval over $250 · idempotency · audit log
                                                        │
                                                        ▼
                                Shopify store (system of record) ⇄ Postgres mirror (Neon), signed webhooks
```
- **Policy gate** (`src/order_ops/gate.py`): the rules a jailbroken model still can't break. A write needs an
  authenticated customer who owns the order and an explicit "yes" in their latest message; each "yes" pays for one
  change; money movements over $250 wait for a supervisor; the same write never runs twice.
- **Router + specialists** (`router.py`): an alternative architecture with context isolation, measured against the
  single agent.
- **Tracing** (`tracing.py`): one OpenTelemetry trace per conversation (model calls with tokens and cost, tool
  calls with the gate's decision, approvals), with no customer text in spans. Phoenix locally, AWS X-Ray in
  production; the supervisor console links each approval to its trace.
- **Evals** (`evaluate.py`, `tau2_run.py`): τ²-bench retail tasks graded on the final database state plus
  natural-language assertions, reported as pass^k. The same agent also runs inside τ²-bench's own orchestrator,
  customer simulator and evaluator (`tau2_adapter.py`), with the gate on or in shadow mode.
- **MCP server** (`mcp_server.py`): the same tools behind the same gate, for any MCP client.

## Run it locally
```bash
uv sync                                  # Python 3.12
scripts/dev_db.sh up                     # Postgres on 127.0.0.1:55433
uv run pytest -q                         # no API calls (scripted model); add --group tau2 to uv sync for the τ² tests
cp .env.example .env                     # ANTHROPIC_API_KEY for the live agent and evals
uv run uvicorn order_ops.api:app --port 8055
cd web && npm install && npm run dev     # http://localhost:3005
```
Evals: `uv run python -m order_ops.evaluate --split test --trials 4` · adversarial: `--split adversarial --gate on|off` · τ²-bench harness: [evals/TAU2.md](evals/TAU2.md).

## Stack
Python, FastAPI, PostgreSQL (psycopg), Anthropic Messages API (manual tool-use loop), τ²-bench, OpenTelemetry,
Next.js + TypeScript + Tailwind, Docker, AWS Lambda (Function URL, SSM, EventBridge Scheduler, X-Ray), Neon,
Vercel, Shopify Admin GraphQL API + webhooks, GitHub Actions (OIDC deploys), MCP.

## Data
τ²-bench retail domain (Sierra Research, MIT license), pinned to v2.0.0: see [data/tau2/SOURCE.md](data/tau2/SOURCE.md).
