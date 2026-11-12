# Order Ops Agent

A customer-service agent that cancels, refunds, exchanges and edits orders for an online store, under a written
policy, with the risky parts enforced in code instead of trusted to the model.

**Live demo:** https://order-ops-agent.vercel.app (a real Shopify dev store; the supervisor console approves refunds
over $250) · **Decisions:** [DECISIONS.md](DECISIONS.md) · **Client brief:** [CLIENT_BRIEF.md](CLIENT_BRIEF.md)

## Results
Every number comes from a recorded run in `evals/` (full tables in [evals/RESULTS.md](evals/RESULTS.md)). Agent,
simulated customer: Claude Haiku 5.5; judge: Claude Sonnet 5.5. Runs used the Claude Agent SDK on a Claude plan in an
isolated configuration (D27; one account-level sentence is added to every session), so costs are
API-equivalent, computed from token counts. About 2,500 episodes in total, 0 harness errors.

<!-- results:start -->
| Measure | Setup | Result |
|---|---|---|
| Reliability, own harness | τ²-bench retail test split, 40 tasks × 4 trials, pass^1 / pass^4 | pass^1 77.5% · pass^4 65.0% |
| Reliability, τ²-bench's own harness | all 114 retail tasks × 4 trials (custom configuration, see below) | pass^1 76.1% · pass^4 51.8% |
| Attacks that caused a write | 10 adversarial tasks × 4 trials, gate on vs. shadow mode (D25); target 0 | gate on: 0 of 40 episodes · shadow: 5 of 40 episodes |
| What the policy gate stops | gate on vs. shadow mode on the 40 test tasks, τ² harness | blocked (on): confirmation 3; would block (shadow): confirmation 5; pass^1 on − off -3.1 pts (95% CI -11.9 to +4.4, 40 tasks) |
| One agent vs. router + specialists | same 40 tasks, same harness (D11) | single 76.9% vs routed 83.8%: +6.9 pts (95% CI -1.2 to +15.6, 40 tasks) |
| Grader check | same agent, this repo's harness vs. τ²'s harness, 40 test tasks | pass^1 77.5% here vs 76.9% in τ²; per-task verdicts agree on 92.5% of 40 tasks |
| Chat → voice | same 40 tasks, own harness: voice prompt + speech-to-text style customer messages (D26) | chat 77.5% vs voice 82.5%: +5.0 pts (95% CI +0.0 to +10.0, 40 tasks) |
| Cost | per conversation (agent, simulated customer, judge) | $0.013 per conversation (agent $0.004, customer $0.004, judge $0.005); $0.018 per resolved task |
<!-- results:end -->

τ²-bench numbers are a **custom configuration**, not a leaderboard submission: the agent has a router and a policy
gate, and both the simulated customer and the judge are Claude models (D22). Published scores use other models and
customer simulators and are not directly comparable.

Also measured (D28, D29): a stronger agent model (Sonnet 5.5, 40 tasks × 2) reaches pass^1 83.8% vs 77.5% for Haiku
on the same trials (+6.3 pts, 95% CI -2.5 to +16.3) at about 16× the agent cost per conversation ($0.050 vs
$0.003). Prompt rules written from an error analysis on the train split fixed the failures they targeted but did
not raise overall train pass^1 (83.8% → 81.1% / 82.4%), so the original prompt was kept.

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
- **Voice** (`voice.py`): a phone call through Twilio ConversationRelay into the same agent and gate. A hedged
  "yeah, I guess" doesn't count as consent, ids and emails are read back, and a filler line covers tool time.
  Barge-in keeps only what the caller heard. Runs on the same Lambda through API Gateway WebSocket
  ([evals/VOICE.md](evals/VOICE.md)).
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
Vercel, Shopify Admin GraphQL API + webhooks, Twilio ConversationRelay, API Gateway WebSocket, GitHub Actions
(OIDC deploys), MCP.

## Data
τ²-bench retail domain (Sierra Research, MIT license), pinned to v2.0.0: see [data/tau2/SOURCE.md](data/tau2/SOURCE.md).
