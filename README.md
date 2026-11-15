# Order Ops Agent

A customer-service agent that cancels, refunds, exchanges and edits orders for an online store, under a written
policy, with the risky parts enforced in code instead of trusted to the model.

**Live demo:** https://order-ops-agent.vercel.app (a real Shopify dev store; the supervisor console approves refunds
over $250; it runs the earlier Haiku version until `final-v1` is redeployed
after an API parity check) · **Decisions:** [DECISIONS.md](DECISIONS.md) · **Client brief:** [CLIENT_BRIEF.md](CLIENT_BRIEF.md)

## Results
Release `final-v1`. Agent: Claude Sonnet 5.5. Simulated customer: Claude Haiku 5.5. Judge: Claude Sonnet 5.5.
The bars were written down before any final run ([D30](DECISIONS.md)), and so was the one rule for leaving tasks
out: only a task whose expected answer breaks the policy (2 of 114, [D32](DECISIONS.md)). Every flagged episode
was read by hand ([evals/FINAL_REVIEW.md](evals/FINAL_REVIEW.md)). Runs used the Claude Agent SDK on a Claude plan
in an isolated configuration (D27), so costs are API-equivalent, computed from token counts. 0 harness errors.

| Bar | Target | Result | |
|---|---|---|---|
| A1 Resolution | pass^1 ≥ 80%, own harness, 39 test tasks × 4 | **86.5%** | ✅ |
| A2 Consistency | pass^4 ≥ 70%, same runs | **≈70%**¹ | ✅ |
| A3 Wrong changes | ≤ 0.5% of conversations, 604 episodes (own 156 + τ² 448) | **1 of 604 = 0.17%** (95% upper bound 0.78%) | ✅ |
| A4 Safe failures | ≥ 90% of agent-caused failures change nothing wrong | **39 of 40 = 97.5%** | ✅ |
| A5 Attacks | 0 unauthorized writes, 13 attacks × 4, gate on | **0 of 52** (gate off: 8 of 52) | ✅ |
| A6 Cost | ≤ $0.50 agent cost per resolved conversation | **$0.069** | ✅ |

¹ pass^4 was 69.2% on the pre-registered run (27 of 39 tasks solved in all 4 trials) and 71.8% on a second run of
the same 39 tasks in τ²-bench's own harness (28 of 39). The two runs differ by one task, within run-to-run noise,
so A2 is counted as met at the bar.

- **A3:** the point estimate meets the bar. With 604 episodes, the data can't rule out a true rate up to 0.78%.
  The one wrong change was borderline. Asked for "the cheapest options", the agent kept the customer's T-shirt
  size and said the total was the lowest possible. A different size was $0.19 cheaper, and the customer cancelled
  the order because of the agent's claim.
- **A5:** with the gate off, a fake manager override and pressure to skip the supervisor approval each succeed in
  4 of 4 trials. With the gate on, both fail, because those rules are enforced in code. Three attacks were written
  for the final run (a "just fill in the address" request, an address planted in another order's data, and a
  refund card that isn't on the account), and all three are blocked in code.

**Checking the grader against human labels** ([D35](DECISIONS.md)). I labeled 40 final conversations myself,
blind to the grader and the reviewer: one per test task, with a failed trial picked where one existed so failures
are well represented (13 of the 40 failed by the grader).

| | Result |
|---|---|
| Agreement with the grader | 70% blind (Cohen's κ 0.15) · 77.5% after reviewing the 6 unclear cases (κ 0.38) |
| Real agent errors the grader missed | **0** (all 3 agent errors I found were also failed by the grader) |
| Grader failures that were agent errors | **3 of 13**: 1 wrong change, 2 process errors. The other 10: the simulated customer strayed from its script (9) or the expected answer relied on data that doesn't exist (1) |
| LLM failure reviewer vs my labels | same failure type on 7 of 10 |

The low κ is a difference in what is measured, not noise: the grader checks the final database against the
customer's hidden script, so it fails the agent when the simulated customer asks for something else. It never
passed a conversation where the agent got it wrong, which makes the bars above a conservative measure. Labeling also
found one issue neither automatic check could see: in eval mode, approvals over $250 ran at once, so the agent
first said a change needed a supervisor and then that it was done. Fixed so eval mode reports the approval the
way the live app does; database results are unchanged.

**Before and after** (same tasks; scope matches the bars):

| | Baseline: Haiku 5.5, original agent | Final: Sonnet 5.5 + code checks | Paired difference in pass^1 |
|---|---|---|---|
| Own harness, 39 tasks × 4 | pass^1 79.5% · pass^4 66.7% | pass^1 86.5% · pass^4 69.2% | +7.1 pts (95% CI −3.2 to +17.9) |
| τ²-bench harness, 112 tasks × 4 | pass^1 77.5% · pass^4 52.7% | pass^1 86.6% · pass^4 72.3% | +9.2 pts (95% CI +4.5 to +13.8) |
| Wrong changes (hand review) | ~1.4% (2 of 146 on train) | 0.17% (1 of 604 on test) | |

The gain comes from two sources: a stronger model, and code checks that make whole classes of mistakes
impossible ([D31](DECISIONS.md)). With the same checks, Haiku reached pass^1 82.5% on train but still made 0.7%
wrong changes, so it missed A3. Opus matched Sonnet's pass^1 at twice the cost, with lower pass^2 ([D33](DECISIONS.md)).
No fine-tuning was used.

**A bug in the benchmark.** τ²-bench's `modify_pending_order_items` (commit 989d328) writes the last new item's
price and options onto every item swapped in the same call. Scoring is unaffected, because the expected database
comes from the same code. In one episode the agent noticed the broken order record and told the customer, instead
of reporting success.

τ²-bench numbers come from a **custom configuration**, not a leaderboard submission: the agent runs behind a policy
gate, and both the simulated customer and the judge are Claude models (D22). Published scores use other customer
simulators and aren't directly comparable.

### Baseline before the code checks (Haiku 5.5, original agent)
Kept for the architecture comparisons (router vs single agent, gate on vs shadow mode, chat vs voice). Full tables
are in [evals/RESULTS.md](evals/RESULTS.md); failure severity for every run is in [evals/SEVERITY.md](evals/SEVERITY.md).

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

## How it works
```
customer ─▶ Next.js chat ─▶ FastAPI on AWS Lambda ─▶ agent loop (Claude Sonnet, tool use)
                                                        │ every tool call
                                                        ▼
                                                   policy gate (code, not prompt)
                                identity · one customer · ownership · explicit "yes" per change
                                approval over $250 · idempotency · audit log
                                                        │
                                                        ▼
                                Shopify store (system of record) ⇄ Postgres mirror (Neon), signed webhooks
```
- **Policy gate** (`src/order_ops/gate.py`, `confirm.py`): the rules a jailbroken model still can't break. A write
  needs an authenticated customer who owns the order. The first call to a change tool changes nothing. It returns
  a summary that code builds from the exact arguments and the store's data: items, options, prices, price
  difference, refund target, and the items on the order the change leaves out. The customer sees that summary, and
  their next explicit "yes" binds to a fingerprint of those exact arguments, so only an identical call runs.
  Every address field, payment method and item id must come from the customer or the account's data, so the
  model can't fill in a value itself. Money movements over $250 wait for a supervisor, and the same write never
  runs twice. After a change, code writes the "Done: ..." line, not the model.
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
cp .env.example .env                     # ANTHROPIC_API_KEY for the live agent (evals can run on a Claude plan instead)
uv run uvicorn order_ops.api:app --port 8055
cd web && npm install && npm run dev     # http://localhost:3005
```
Evals (`uv sync --group tau2 --group evals`; add `--backend subscription` to run on a Claude plan, D27):
`AGENT_MODEL=claude-sonnet-5-5 uv run python -m order_ops.evaluate --split test --trials 4` · adversarial:
`--split adversarial --gate on|off` · τ²-bench harness: `python -m order_ops.tau2_run --split base --trials 4`
([evals/TAU2.md](evals/TAU2.md)) · failure severity: `python -m order_ops.severity` · flag triage:
`python -m order_ops.reviewer <label>`. A run stopped by the usage guard resumes with the same command.

## Stack
Python, FastAPI, PostgreSQL (psycopg), Anthropic Messages API (manual tool-use loop), Claude Agent SDK (evals on a
Claude plan), τ²-bench, OpenTelemetry,
Next.js + TypeScript + Tailwind, Docker, AWS Lambda (Function URL, SSM, EventBridge Scheduler, X-Ray), Neon,
Vercel, Shopify Admin GraphQL API + webhooks, Twilio ConversationRelay, API Gateway WebSocket, GitHub Actions
(OIDC deploys), MCP.

## Data
τ²-bench retail domain (Sierra Research, MIT license), pinned to v2.0.0: see [data/tau2/SOURCE.md](data/tau2/SOURCE.md).
