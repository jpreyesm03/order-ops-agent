# Decisions

Each entry: the question, the options, what was chosen and why. Results that confirm or overturn a decision are
added under it as they come in.

## D1. Benchmark: τ²-bench retail, not τ³ or my own tasks
- **Options:** τ²-bench retail · τ³-bench (newer, 75+ task fixes, adds banking) · write my own tasks.
- **Chosen:** τ²-bench retail, pinned to tag `v2.0.0`.
- **Why:** it is the most cited agent benchmark for customer service, with published baselines to compare against. Its
  tasks score the **final database state**, which is objective and needs no judge for the main check. τ³ has
  fewer published numbers; own tasks would have no external reference.
- **How it's used:** data and scenarios only (store DB, policy, tasks). Tools, agent, simulator and evaluator are
  my own code, so the result is "my agent on their tasks".

## D2. Postgres (relational), not the benchmark's in-memory JSON
- **Options:** keep the JSON dict in memory like τ² · SQLite · Postgres.
- **Chosen:** Postgres, normalized tables (users, payment_methods, products, variants, orders, order_items,
  order_payments, order_fulfillments) + operational tables (approvals, idempotency_keys, audit_log, sessions).
- **Why:** a real deployment sits on a real database. Transactions make each write atomic, the approval queue and
  audit log need durable shared state, and the console queries orders with SQL.
- **Check:** loading `db.json` into Postgres and exporting it back gives a byte-identical store (test
  `test_round_trip_matches_source_json`).

## D3. Money as NUMERIC(12,2), never float
- **Why:** float arithmetic drifts (0.1 + 0.2 ≠ 0.3). Gift-card balances and refunds are computed in `Decimal`
  and stored as `NUMERIC`; floats appear only when exporting JSON for the model and for state comparison.

## D4. One Postgres schema per eval episode
- **Options:** one transaction per episode with rollback · reload the store per episode · clone a schema per
  episode.
- **Chosen:** clone `base` into `ep_<id>` (≈0.1–0.5 s), run the episode, export, drop.
- **Why:** long-lived transactions would hold row locks, so parallel episodes touching the same order (e.g. 4 trials
  of one task) would block each other. Separate schemas are fully isolated and allow 8+ episodes in parallel.

## D5. Policy rules in code (the gate), not only in the prompt
- **Context:** τ²'s write tools only check *data* rules (order status, item counts). Identity, confirmation and
  "one customer per chat" exist only as prompt text, so a manipulated model can break them.
- **Chosen:** `gate.py` wraps every tool call: identity → one customer → ownership → explicit yes → approval
  limit → idempotency → audit.
- **Why:** a prompt can be talked around; code can't. A jailbroken model still cannot refund another customer's
  order, act without a "yes", or refund twice.

## D6. Confirmation: deterministic check on the customer's latest message, one yes = one change
- **Options:** trust the model to ask · an LLM classifier for "did the customer confirm?" · a regex on the
  customer's latest message.
- **Chosen:** regex (yes / confirm / go ahead / sounds good…, rejected if the message starts with no / wait /
  don't). The "yes" is consumed by the first successful write; a failed write keeps it so the agent can retry with
  fixed arguments.
- **Why:** deterministic, auditable, zero cost, and a model can't argue with it. The trade-off is false negatives
  ("fine by me") that cost an extra turn; they are counted in evals as `blocked_by_gate.confirmation`.
- **Security detail:** the check reads only the customer's raw new message. In router mode, the recap of earlier
  turns passed to a new specialist may contain an old "yes"; it is never counted (test
  `test_router_recap_yes_is_not_a_confirmation`). After a mid-turn handoff, the same "yes" isn't counted twice.

## D7. Human approval above a threshold ($250 default)
- **Chosen:** writes that move more than the threshold (refund, charge or transfer) are stored in `approvals` and
  run only when a supervisor approves, exactly once, via the same idempotent path.
- **In evals:** approvals are counted and auto-approved, so the final state can still match the benchmark's.
- **Why:** this is how real CX teams roll out automation: autonomous below a limit, human above it, raising the
  limit as reliability data comes in.

## D8. Idempotency per (session, tool, arguments)
- **Why:** retries happen (timeouts, a model repeating itself). The key and the write commit in one transaction,
  so a repeated call returns the stored result instead of refunding twice (test `test_cannot_refund_twice`).

## D9. Fixed a bug in τ²'s reference tool
- `modify_pending_order_items` in τ² reuses the last new variant's price and options for every swapped item. This
  repo swaps each item to its own variant. Gold states are produced by replaying the gold actions through this
  repo's tools, so the comparison stays fair.

## D10. Model: Claude Haiku 5.5 for agent and simulator, Sonnet 5.5 as NL judge
- **Why:** Haiku 5.5 costs $0.10 / $0.50 per million tokens, so a full pass^4 run costs a few dollars, and a
  cheap model is what a client would run at 3,000 tickets/month. The judge only grades the 40 tasks with
  natural-language assertions, so a stronger model there is cheap.
- Prompt caching on tools + system + conversation; `disable_parallel_tool_use` (the policy says one call at a
  time); manual tool loop (not the SDK runner) so every call goes through the gate.

## D11. Router + specialists as an option, measured against one agent
- **Design:** a cheap router call (structured output) picks orders / returns / account. Each specialist has its
  own context, only its tools and only its slice of the policy, plus a `handoff` tool if it's the wrong one.
- **Why measure, not assume:** smaller contexts and tool sets can help a small model, but routing errors and lost
  context can hurt more. Both run on the same tasks with `--arch single|routed`.
- **Result:** pending.

## D12. Next.js + FastAPI, not Streamlit
- **Why:** the console is a product a supervisor would use (approval inbox, audit log), not a notebook. It also
  shows TypeScript/React. The browser calls FastAPI directly (CORS), with rate limits per IP and per day to protect
  the API key on a public demo.

## D13. Hosting: Vercel (web) + AWS App Runner (API, Docker) + Neon (Postgres)
- **Why:** Vercel is the native home for Next.js and free. The API ships as a Docker image (multi-stage, non-root)
  to AWS. Neon gives serverless Postgres 18 at $0 for this size, same version locally and in CI.

## D14. MCP server
- The same tools behind the same gate, for any MCP client. Writes take the customer's confirming words as an
  argument, checked by the same rule; MCP clients also ask their user to approve each call. Tools carry
  read-only / destructive annotations.

## D15. Dev environment isolation
- Local Postgres runs on Colima (a separate Docker engine) under its own compose project on 127.0.0.1:55433,
  and the code refuses any database not named `orderops_p5`. Reason: the machine also runs unrelated containers
  that must never be touched.
- The repo lives outside the cloud-synced folder (sync evicted files and hung Python); a git bundle + snapshot is
  copied there after commits.
