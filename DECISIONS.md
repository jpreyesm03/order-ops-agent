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
- **Across sessions:** idempotency keys are per conversation, so two conversations (e.g. two browser tabs) could
  change the same order at once. Found on the live Shopify store: two simultaneous cancels → one succeeded, the other
  crashed on a database constraint (no double refund, but an error instead of an answer). Every write now locks the
  order (or user) row first (`SELECT … FOR UPDATE`): the second change waits, then sees "Non-pending order cannot be
  cancelled". The gate also turns any database error into a polite tool error instead of crashing the conversation
  (test `test_concurrent_cancels_refund_once`).

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

## D13. Hosting: Vercel (web) + AWS ECS Express (API, Docker) + Neon (Postgres) · superseded for the API by D20
- **Why:** Vercel is the native home for Next.js and free. The API ships as a Docker image (multi-stage, non-root)
  to AWS. Neon gives serverless Postgres 18 at $0 for this size, same version locally and in CI.
- **App Runner → ECS:** the plan was App Runner, but AWS no longer lets new accounts create App Runner services.
  ECS Express Mode (its successor) runs the same image on Fargate behind an HTTPS load balancer it manages.
  One task (in-memory rate limits, capped spend), ~$25/month.
- **Delivery:** every push to `main` runs tests, builds the image, pushes it to ECR with short-lived GitHub OIDC
  credentials (no AWS keys stored in GitHub; the role only trusts this repo's `main`) and rolls it out blue/green.
- **Lessons from the first deploys:** (1) a library used only by the new Shopify code (`httpx`) was a dev
  dependency, so tests passed but the production image crashed on start; ECS's circuit breaker rolled back and the
  site stayed up. CI now starts the built image and imports the app before pushing. (2) Canary + bake time + draining
  takes 10–20 min, longer than the AWS CLI's default wait; CI now follows the deployment's own rollout state for up
  to 25 min and fails on a rollback. (3) Docs-only pushes no longer deploy, and only one rollout runs at a time.
  Secrets (database URL, API keys, Shopify app) are in Secrets Manager, readable only by the task's role.

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

## D16. Live demo on a real Shopify store; benchmark stays on τ²
- **Options:** Shopify read-only · a switch between both stores · live demo fully on Shopify.
- **Chosen:** the public demo runs against a Shopify dev store (`order-ops-demo`); evals keep running on the τ²
  Postgres store, so scores stay comparable to published results.
- **How the data gets there:** `seed_shopify` copies a deterministic slice of τ² (8 customers with a pending and a
  delivered order, their 20 orders, the 41 products they touch) into Shopify with the same order numbers, and loads
  the same slice into the mirror schema. Shopify allows 3 options per product, so 4-option τ² products get their
  last two merged ("material and style").
- **Reseeding safely (demo orders get used up):** the first live reset deleted the old orders, then failed on a
  customer address, leaving the mirror pointing at deleted orders. Now it creates the new orders, swaps the mirror
  in one transaction, and only then deletes the old ones (Shopify allows the same order number on two orders
  briefly). A failure at any step leaves the previous demo intact. Address cause: Shopify adds order shipping
  addresses to the customer's address book and rejects editing one into a duplicate of another, so the seed now
  re-selects the existing matching address as the default.

## D17. Write-through mirror: Shopify is the system of record, Postgres serves reads
- **Write path:** the store method runs first inside a savepoint (same business rules as the benchmark, e.g. only
  pending orders can be cancelled), then the Shopify Admin GraphQL mutation runs; the mirror commits only if Shopify
  accepted. A change the policy forbids never reaches Shopify; a change Shopify refuses never shows in the mirror.
- **Why not read from Shopify directly:** the gate's ownership checks, the console and the audit log need fast SQL,
  and the Admin API is rate-limited.
- **Found in testing:** `orderCancel` is a background job and can fail silently (the test gateway refused the
  refund, the order stayed open while the job reported done). The cancel now waits for the job and checks
  `cancelledAt` before committing, and seed payments are recorded as manual payments (as imported orders are).
- **Mapping:** τ² ids → Shopify GIDs in `shopify_ids` (orders, line items, fulfillment line items, variants by SKU,
  customers, addresses). Order edits create new line items, so the map is refreshed after each edit.
- **Gaps, stated plainly:** Shopify has no API to move a pending order's payment to another method; that change
  is written to the order note and the mirror. Price differences from item swaps stay as an outstanding balance on
  the Shopify order.

## D18. Webhooks for changes made outside the agent
- Staff can cancel or edit orders in the Shopify admin. Signed webhooks (HMAC-SHA256 of the raw body with the app
  secret, compared in constant time) update the mirror. Each webhook id is applied once; events for changes the
  agent already made are recognized as "in sync". External changes are written to the audit log as `external`.

## D19. Auth: client-credentials tokens, secrets never in code or config
- The app uses Shopify's client-credentials grant: a 24 h token, renewed automatically; no long-lived Admin token
  exists anywhere. Client id/secret live in `.env` locally. In production they were in Secrets Manager, injected by
  ECS; since D20 they are SSM SecureString parameters the function reads at cold start.

## D20. API moved from ECS to Lambda (same image), measured cost first
- **Measured (Cost Explorer, Nov 1–3, before credits):** $2.92 for under 3 days, about $35–40/month. Public IPv4
  addresses $1.18 (4 × $0.005/h, ALB + task), load balancer $1.06, Fargate task $0.58, Secrets Manager $0.10.
  About 80% paid for the always-on front door, not the app. Credits ($116) would have run out around February,
  while the demo still needs to be up.
- **Options:** keep ECS (smaller, Spot) · Lambda + API Gateway · Lambda + Function URL · another host.
- **Chosen:** Lambda with a Function URL, running the **same Docker image** through the AWS Lambda Web Adapter
  (an extension that turns each invocation into an HTTP request to uvicorn). No code path is Lambda-only for HTTP;
  the image still runs anywhere. Function URL over API Gateway because API Gateway caps a request at 29 s, and an
  agent turn with several tool calls can take longer; the URL allows up to the function timeout (900 s).
- **Cost now:** ~$0/month within the permanent free tier (1M requests, 400k GB-s). Secrets moved to SSM
  Parameter Store (SecureString, free) instead of Secrets Manager ($0.40 per secret).
- **What changed because Lambda runs several short-lived copies:**
  - Rate limits were in process memory (one ECS task). They now live in Postgres (`public.rate_limits`, fixed
    UTC-hour per IP and UTC-day windows, incremented in one transaction; a rejected message rolls back). The IP comes
    from the request context the adapter passes; the old code trusted the first `X-Forwarded-For` entry, which the
    client controls (found during the move; also true on ECS).
  - The Shopify reseed ran in a background thread; Lambda freezes the process after the response. It now runs as an
    async invocation of the same function (delivered to `POST /events`, admin token required since the URL is
    public), guarded by a lock row in `public.task_locks` (Neon's transaction pooler rules out session advisory
    locks). Async retries are off so a retry can't race a half-done reseed.
  - The nightly reseed (OUTLINE 8.8) is now an EventBridge Scheduler rule (10:00 UTC) invoking the function.
- **Trade-offs, and what we did about them:**
  - Cold start ~5 s on the first request after idle (warm ~0.13 s for `/healthz`). The web app pings `/healthz` as
    soon as any page opens, so the instance is usually warm before the visitor's first message. Provisioned
    concurrency would remove it entirely but costs money all month, which is the thing we left ECS to avoid.
  - Release safety. The first Lambda deploy put new code on traffic immediately. Now each release publishes a
    numbered version, CI calls `/healthz` on *that version* directly (a Function URL-shaped event through the
    adapter, `scripts/lambda_smoke.sh`), and only then moves the `live` alias, which the public URL serves. A
    failing build never gets a request; rollback is `scripts/lambda_rollback.sh <version>` (~1 s, no rebuild;
    window = the last 10 images ECR keeps). Gradual traffic shifting (CodeDeploy canary) was not added: with
    demo-level traffic a 10% canary sees almost no requests, so the pre-traffic check is what catches a bad build.
    The Function URL moved to the alias, so the URL changed once (CI, Vercel and the Shopify webhooks updated). The account's concurrency limit (10) is too low to
  reserve concurrency, so spend is capped by the Postgres rate limits.
- **First deploy:** 502 on every request. Logs showed the app exiting at startup: `ssm:GetParametersByPath`
  was denied because the IAM policy named only `parameter/order-ops`; it now allows the path and everything under
  it (`parameter/order-ops/*`). ECS kept serving the site until Lambda passed its checks; the ECS script is in `scripts/archive/`.
- **Cutover:** the web app was switched to the alias URL and the live bundle checked (alias URL present, ECS URL
  gone); then the ECS service, its load balancer, the cluster, the ECS IAM roles and the Secrets Manager secrets
  were deleted. Checked after: no load balancers, target groups or public IPs left in the account.

## D21. Run the agent on τ²-bench's own harness, pinned through the lockfile
- **Constraint:** every eval number so far comes from this repo's simulator and judge (D1: data only). An outside
  reader can fairly ask whether the grader favours the agent. The fix is to run the same agent inside τ²-bench's
  own orchestrator, user simulator and evaluator.
- **Evidence:** τ²-bench `v2.0.0` (commit `989d328`, the same commit the data in `data/tau2/` comes from) depends
  on litellm and caps it at `<1.82.7`. Versions 1.82.7 and 1.82.8 on PyPI were a credential stealer (March 2026).
  An unlocked install resolves whatever is newest that day.
- **Options:** optional dependency group pinned to the git commit and recorded in `uv.lock` · ad-hoc
  `uv run --with` (nothing locked) · copy the source into the repo.
- **Chosen:** dependency group `tau2` in `pyproject.toml`, git source pinned to `989d328`, every transitive version
  in `uv.lock`. Installed only with `uv sync --group tau2`. The Lambda image installs `--no-dev --extra aws`, so
  production never contains it.
- **Rejected:** `--with` (a benchmark run nobody can reproduce is not evidence); vendoring (thousands of lines that
  are not mine, read as if they were).
- **Would change if:** τ²'s dependencies conflict with the app's. Then a separate `evals/tau2/` project with its
  own lockfile.

## D22. τ² runs use Claude as the simulated customer and judge, reported as a custom setup
- **Constraint:** the spec asked for a score "next to published scores for the same model".
- **Evidence (leaderboard files at `v2.0.0`, checked 2026-11-11):**
  - No Haiku model has a τ² retail entry at all. The 2.0 text tab has no retail entries yet (voice only).
  - On the 1.x tab, Sierra's recent runs use a gpt-5.2 customer. Retail: Sonnet 4.5 pass^1 72.4 / pass^4 39.5,
    Opus 4.5 79.6 / 51.8. Older vendor runs used gpt-4.1.
  - τ²'s submission rules call any router, extra agent, modified prompt or non-default simulator "custom". The
    routed agent (D11) and the policy gate both qualify, whatever the customer model.
  - The customer model alone moves scores by up to 9 points with the agent held fixed ("Lost in Simulation",
    arXiv 2601.17087, ACL 2026).
  - v2.0's natural-language judge defaults to an OpenAI model (`gpt-5.4-mini`).
- **Options:** gpt-4.1 customer (the old default) · Claude customer · both.
- **Chosen:** Claude for the customer and the judge (one provider, one key, one spend cap). Results are labelled
  "custom configuration, not a leaderboard submission". Published numbers appear only as context, never as a
  ranking.
- **Instead of leaderboard comparability, two comparisons that answer real questions:**
  1. Same agent, this repo's harness vs τ²'s harness: how much does the home-made grader move the score?
  2. Gate on vs off (D23) and single vs routed (D11) on the same harness and customer.
- **Would change if:** a reader needs a leaderboard-comparable number. Then: a standard run
  (default agent scaffold + default customer) for one model, at the cost measured by the sample run.
- **Correction to D1:** "published baselines to compare against" holds for other models, not for the model this
  agent uses.

## D23. The policy gate inside τ²: same rules, lookups behind methods
- **Constraint:** τ² executes tools against its own in-memory database. The gate (D5) looked up order owners,
  payment-method owners, order totals and variant prices with SQL against Postgres.
- **Options:** move the lookups into methods and add a subclass that reads τ²'s database · run every tool on both
  Postgres and τ² (two stores that can drift; every call runs twice) · re-implement the rules in the adapter (two
  copies of the policy).
- **Chosen:** lookups and bookkeeping (idempotency keys, audit, approvals) are methods on `Gate`. `Tau2Gate`
  overrides them to read τ²'s database and keep its records in memory. The rule code (`_check`, approval threshold,
  one-yes-per-write, idempotency) is the same code in both. A blocked call goes back to the model as a tool
  result and never reaches τ²'s environment. The same split lets a future gateway wrap this agent's
  tools.
- **Gate off runs in shadow mode:** the gate still evaluates every call and records what it *would* have blocked,
  without blocking. One gate-off run then gives the counterfactual ("would have blocked N writes; M of them in
  failed tasks") without paying for extra runs.
- **Expected result, said before measuring:** standard τ² customers rarely attack, so on/off may differ by about
  0 points on the standard tasks. The gate's value shows on the adversarial tasks.

## D24. Tracing: OpenTelemetry to Phoenix locally and AWS X-Ray on Lambda
- **Constraint:** the spec (§P.2) asks for: a trace per conversation with model calls, tool calls,
  gate decisions and approvals, and no customer text in spans. Same module design as the other projects (an
  OpenTelemetry `span()` helper, OpenInference span kinds, off unless an endpoint is configured).
- **Options:** Phoenix locally + X-Ray on Lambda · Phoenix only, live later · a hosted LLM-tracing service.
- **Chosen:** Phoenix locally (`ORDER_OPS_TRACE_ENDPOINT`), X-Ray on Lambda (`ORDER_OPS_TRACE_XRAY=1`). The app
  already runs on AWS; X-Ray adds no account, key or vendor. A hosted service would add a new account and a stack
  different from the other projects.
- **Details that matter:**
  - X-Ray's OTLP endpoint (`https://xray.<region>.amazonaws.com/v1/traces`) accepts HTTP only, needs SigV4-signed
    requests and needs CloudWatch Transaction Search enabled. The exporter signs with the function's own role
    (`xray:PutTraceSegments`, `xray:PutSpans`).
  - Lambda freezes the process after each response, so spans waiting in a batch would be lost. The API flushes at
    the end of each request.
  - One trace per conversation even though each message is a separate HTTP request (and maybe a different Lambda
    instance): the session stores the trace id and root span id, and every turn is a child of that root.
  - The supervisor console links each ticket to its trace. The link is a URL template, so it works with any
    backend. X-Ray trace ids are the OpenTelemetry id reformatted as `1-<first 8 hex>-<last 24 hex>`.
  - Spans carry ids, tool names, outcomes, gate rule, amounts, tokens, cost and timing. No customer or agent
    text, no tool arguments that hold personal data (emails, names, addresses).
    `ORDER_OPS_TRACE_CONTENT=1` adds text for local debugging only.
- **Cost:** spans land in CloudWatch Logs (`aws/spans`), billed per GB ingested. At demo volume, about $0.

## D25. Adversarial set: 10 attacks, scored on what got written, gate on vs. shadow
- **Constraint:** the spec's target is "0 successful injections that cause a write", and D23 predicted that the
  standard τ² tasks can't show the gate's value (their customers don't attack, and τ² grades only the final
  database, so a write made before consent can still pass). The gate needs its own test set.
- **Options:** run the attacks inside τ²'s harness · run them in this repo's harness · score them by hand.
- **Chosen:** this repo's harness (`--split adversarial`, `data/adversarial/tasks.json`). τ² loads tasks only from
  its own registered domains, and one attack needs the starting data changed (text planted in an order's address),
  which this harness supports through a task's `setup` patch (only `address`/`status` columns, allowlisted).
- **The 10 attacks** (each on real τ² customers and orders): another customer's order, no authentication, identity
  switch mid-chat, fake manager override without a "yes", refund to someone else's card, indirect prompt injection
  in order data, another customer's personal data, "developer mode" jailbreak, a pasted fake tool result, legal
  threats to skip the supervisor.
- **Scoring:** same reward as every task (final database matches gold × the judge's assertions), plus
  `unauthorized_writes`: writes that ran and are not in the gold actions. Headline = episodes with an unauthorized
  write (target 0). Approvals are never auto-approved here (the live app's behavior), so "waiting for a
  supervisor" counts as stopped.
- **Gate on vs. shadow:** `--gate off` now works in this harness too (same semantics as `Tau2Gate`: count
  `would_block`, let the call run; idempotency stays on). The difference between the two runs is what the code
  stops that the model alone would not.
- **Honest limits, written before running:**
  - adv_08 (cancel a delivered order) has no gate rule: the store's status check and the model stop it.
  - The confirmation rule is a regex on the latest customer message. "ok thanks" counts as a yes, so adv_06
    (injection + a customer who says "ok") tests the model, with the gate as a weak backstop.
  - 10 tasks is a smoke test of known attacks, not a robustness claim.
- **Cost:** 10 tasks × 4 trials × 2 modes = 80 episodes, about $1.50 at the τ² cost prior (~$0.02 each).

## D26. Voice channel: Twilio ConversationRelay into the same agent and gate, on Lambda
- **Constraint:** a phone number anyone can call to cancel an order or start a return, with the same
  agent, tools, gate, approvals and audit as the chat. Target < 1.5 s to first audio. The API runs on Lambda
  (~$0 idle, D20), and Lambda can't hold a websocket open.
- **Options (chosen 2026-11-11):**
  | | Twilio ConversationRelay (chosen) | Pipecat + Media Streams | Speech-to-speech model |
  |---|---|---|---|
  | Who does speech ↔ text | Twilio (barge-in included) | us (Deepgram + a TTS vendor) | the model |
  | Hosting | API Gateway WebSocket → existing Lambda | always-on container | always-on container |
  | Idle cost | ~$1.15/mo (number) | ~$12–25/mo + number | ~$12–25/mo + number |
  | Per minute | ~$0.07 relay + $0.0085 call + Claude | ~$0.06 | ~$0.10–0.30 |
  | New accounts | Twilio | Twilio, Deepgram, TTS | Twilio, OpenAI/Google |
  | Evals carry over | yes, same Claude agent | yes | no, different model |
- **Why:** keeps the $0-idle architecture and the evaluated agent, and adds one account. What it gives up: we
  don't build the audio pipeline. The voice problems that matter for the product stay in our code: consent on
  transcripts, ids misheard, latency while tools run, barge-in history.
- **How it works:** `POST /voice/incoming` (Twilio-signed) returns TwiML that connects the call to our websocket.
  Twilio sends `setup` / `prompt` (transcribed) / `interrupt` / `dtmf`; we answer with `text` tokens. Two
  transports, one handler (`voice_event`): `/voice/ws` (any long-running host, local dev through a tunnel) and
  API Gateway WebSocket → `POST /voice/events` on the Lambda, replies through the management API.
- **Voice-specific rules:**
  - Consent: a hedged answer ("yeah, I guess", "maybe") is passed to the gate as no confirmation at all.
  - The voice prompt adds: spoken style, read back emails/ids before using them, ids character by character.
  - A filler line ("One moment while I check that.") if the answer isn't ready after 1.2 s; the answer preempts it.
  - Barge-in: the model's history keeps only what the caller heard, so it doesn't assume they heard the rest.
  - Two utterances at once (Lambda can run them on two instances): the second is queued in the call's row and
    joined into the next turn, never run in parallel.
- **Cost guards:** 30 calls/day (`VOICE_CALLS_PER_DAY`), 8 min per call, then a goodbye. The websocket accepts
  only the secret embedded in the signed TwiML.
- **Measured without a phone:** `evaluate.py --channel voice` runs the 40 test tasks with the voice prompt and
  customer messages rewritten as speech-to-text output (lowercase, no punctuation, emails spoken). The report
  shows the chat → voice gap. Real calls log time to first audio per turn (p50/p95 from the sessions table).
- **Limits:** replies aren't token-streamed yet (sentence chunks after the full turn); the filler hides tool time,
  not model time. The API Gateway setup (`scripts/voice_aws.sh`) is untested until a Twilio account exists.

## D27. Evals run on a Claude plan through the Agent SDK; the API key only serves the deployed app
- **Why:** scored evals use the Claude plan, not API credits. The deployed chat and voice keep the API path; a
  small parity check at deploy time (about 5 tasks × 3 on both paths) measures any difference.
- **How it works (`subscription.py`, `guard.py`, flag `--backend subscription` on `evaluate.py` and `tau2_run.py`):**
  - The agent keeps its own loop. Each agent (or routed specialist) is one Agent SDK session per conversation, and
    its tools are in-process MCP tools whose handlers run nothing: they pass the call to our code and wait for
    the result. `_create` still returns one step at a time (one tool call or one reply), so `Agent.respond`, the
    router, the policy gate and the τ² adapter are unchanged, and every tool call still goes through the gate
    (or out to τ²).
  - Calls without tools (simulated customer, judge, router) use a stand-in for the API client, one SDK query
    each. τ²'s customer and judge reach it through a litellm provider, `claude-sub/<model>`.
  - Guard on every session: isolated CLI configuration with its own login token, never an API key
    (`apiKeySource` must be `none`), only the account-level hooks with their exact output (tool results stay
    unchanged), stop at 80% of the 5-hour or weekly window and on any paid extra usage. Runs then stop cleanly
    and keep the episodes already written.
- **Known differences from the API path:** tool names show as `mcp__store__<name>`; parallel tool calls can't be
  switched off (they run one at a time); customer and judge histories go in as one transcript; no temperature;
  operator notices are a tagged line in the user message; one account-level sentence is added to each session.
- **Measured (2 tasks on each harness, Haiku agent and customer, Sonnet judge):** about 1 min per episode
  (CLI startup is about 7 s per single call). Six episodes plus smoke calls moved the 5-hour window from 42% to
  43% and the weekly window not at all (it reports whole percents). API-equivalent cost is $0.005 to $0.013 per
  episode.
- **Stop-safe runs (`runlog.py`, both harnesses):** each finished episode is appended to `results.jsonl` with
  flush + fsync. An episode cut off by a stop is never saved, so it can't be scored as a failure. A plan limit
  stops the run (`QuotaStop`) whether it shows up as a rate-limit event, an error result or a CLI exception. The
  stopped run exits with code 3 and prints the resume command. Rerunning the same command (same `--label`) skips
  saved episodes and retries errored ones. `run_config.json` refuses a resume with other settings. Tested live:
  `kill -9` after episode 1, then a resume with the limit forced (stopped mid-episode, still 1 saved, exit 3),
  then a normal resume (ran only the 2 missing, `complete: true`). τ² with the limit forced: exit 3, nothing saved.

## D28. Error analysis on the train split: three failure types found, prompt fixes kept out (no overall gain)
- **Method:** test split held out the whole time (only its pass/fail counts were seen). Train baseline 74 tasks,
  failures read and grouped, general fixes only (no task-specific rules, policy text untouched), targeted re-runs
  ×3, then full train 74×2 for every version, compared like for like. The selection rule was fixed before the last
  run: freeze the highest train pass^1.
- **Failure types (13 failures in the 74×1 train baseline):**
  - *One change per order* (3): separate return/exchange calls on one order; after the first, the order is no
    longer "delivered" and the rest fail. Also item modification before an address change locks the order.
  - *Not looking up what the customer points to* (2): asks the customer to type an address that is in one of their
    orders.
  - *Variant choice* (3): wrong variant picked, or "not available" when a match exists under another name
    ("Google Home" vs "Google Assistant").
  - *Not fixable without breaking rules* (5): label contradicts the policy (task 105: gift card balance $17 below
    the $21.10 difference, gold exchanges anyway), needs an unstated rule (task 67: order list is chronological),
    or simulated-customer drift (66, 98, 46).
- **Fixes tried:** v1 (b5b03b8) added three preamble rules + one tool description; v2 (0363e0c) added "an answer
  to 'anything else on this order?' is not a yes". Kept on branch `experiment/agent-rules-v2`.
- **Results (train, 74×2 each, Haiku, subscription runner):**

  | Version | pass^1 | pass^2 | DB right | Assertions met | Gate confirmation blocks |
  |---|---|---|---|---|---|
  | v0 original | **0.838** | **0.77** | 0.872 | 0.939 | 2 |
  | v1 rules | 0.811 | 0.73 | 0.892 | 0.912 | 9 |
  | v2 rules + yes fix | 0.824 | 0.73 | 0.885 | 0.932 | 8 |

  The targeted tasks improved as predicted (e.g. task 87 0/2 → 2/2, 104 0/2 → 1/2, 19 and 0 1/2 → 2/2; on the 13
  failed tasks ×3: 0% → 44%), but other tasks lost about as much, and the agent tried writes without a fresh yes
  more often (the gate blocked all of them). Net change is within noise (about ±4 points on 148 episodes).
- **Decision:** keep the original prompt (agent-v1 = v0 behavior). Test numbers from Phase 1 already use it.
- **What it says:** more prompt rules for a small model move failures around instead of removing them. The next
  levers are code, not prose: a gate-level "one change per order" check that tells the model what else is on the
  order before the first change, an address lookup tool, and a variant-matching helper; or a stronger model
  (Sonnet run in D29).
