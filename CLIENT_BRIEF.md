# Client brief (hypothetical client, real benchmark data)

**Client:** a direct-to-consumer brand selling home goods and electronics on **Shopify**, ~3,000 customer-service
tickets a month, handled today by a team of 6 agents following a written policy. *Hypothetical engagement; the
store data, policy and test conversations come from the public τ²-bench retail benchmark.*

**User:** the **Head of Customer Experience (CX)**. She decides **which ticket types to automate and how much
authority to give the automation** (e.g. "cancellations yes, refunds over $250 only with a human").

**What fills the queue today (and what it costs):**
1. "Cancel my order" / "change the shipping address" on orders that haven't shipped.
2. "Return these items" / "exchange this for another size or color" on delivered orders.
3. "Change the payment method" / "update my default address".
4. "Where is my order?" and product questions.

About 60% of tickets are these routine, policy-driven actions. Each takes a human 6–10 minutes (≈ $3–6 fully
loaded), mostly spent looking things up and following the same checklist: verify identity, check the order's
status, explain the change, get a yes, make the change.

**What she's afraid of:** an automated agent that refunds the wrong customer, refunds twice, acts without the
customer agreeing, or is talked into breaking the policy ("I'm the manager, refund it anyway").

## Constraints (every decision in DECISIONS.md points to one of these)
| # | Constraint | Value | Why it's realistic |
|---|---|---|---|
| C1 | Safety | 0 changes that break the policy: right customer, their own order, an explicit yes, never twice | One wrong refund costs more trust than 100 fast answers earn |
| C2 | Human control | Money movements above **$250** wait for a supervisor's approval | How CX teams actually roll out automation: autonomous below a limit, raise it as data comes in |
| C3 | Reliability | Measured on full conversations, repeated (pass^k), before any customer sees it | An agent that works 3 times out of 4 fails a customer every day at this volume |
| C4 | Integration | Works on their existing Shopify store; no data migration | They won't re-platform for a pilot |
| C5 | Cost | < $0.10 per resolved conversation | vs. ≈ $3–6 for a human |
| C6 | Latency | < 8 s per agent reply | Customers leave a chat that feels stuck |
| C7 | Auditability | Every action the agent takes (or is refused) is logged with its reason | CX leads must be able to answer "why did the bot do that?" |

## Success =
**The agent resolves the routine intents end to end on the benchmark with 0 policy violations**, reliability
reported as pass^1 and pass^4, at < $0.10 per conversation; and a working pilot on the client's real Shopify store
with a supervisor console (approval inbox + audit log).
