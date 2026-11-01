import evals from "@/data/evals.json";

type EvalSummary = { status?: string; [k: string]: unknown };

const RULES = [
  ["Identity", "Account data and every change require the customer to be found by email, or by name + zip code."],
  ["One customer", "After verification, lookups that resolve to someone else are blocked for the rest of the chat."],
  ["Ownership", "Orders, profiles and payment methods must belong to the verified customer."],
  ["Explicit yes", "A change runs only if the customer's latest message is an explicit confirmation. One yes, one change."],
  ["Human approval", "Money movements above the limit are queued for a supervisor instead of executed."],
  ["Idempotency", "The same change with the same arguments in one conversation runs once. No double refunds."],
  ["Atomic writes", "Each change is one database transaction: it fully applies or leaves no trace."],
  ["Audit log", "Every tool call is recorded with its outcome: ok, error, blocked, pending approval or duplicate."],
];

export default function HowItWorks() {
  const e = evals as EvalSummary;
  return (
    <article className="max-w-3xl space-y-8">
      <section className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">How it works</h1>
        <p className="text-muted">
          Most demo agents only read data. This one changes it, so the question is not &ldquo;is the model smart&rdquo;
          but &ldquo;what stops it from doing damage when it&apos;s wrong or manipulated&rdquo;. The answer here: the
          rules live in code around the tools, not only in the prompt.
        </p>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-medium">Architecture</h2>
        <pre className="overflow-x-auto rounded-lg border border-line bg-panel p-4 text-xs leading-relaxed">{`customer ─▶ Next.js console ─▶ FastAPI ─▶ Claude agent loop
                                              │ one tool call at a time
                                              ▼
                                        policy gate (code)
                    identity · ownership · explicit yes · approval limit · idempotency · audit
                                              │
                                              ▼
                              Postgres store (orders, payments, approvals, audit log)
                                              ▲
                         supervisor console ──┘ approve / reject queued refunds`}</pre>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-medium">The rules enforced in code</h2>
        <dl className="divide-y divide-line rounded-lg border border-line bg-panel text-sm">
          {RULES.map(([k, v]) => (
            <div key={k} className="grid gap-1 px-4 py-2 sm:grid-cols-[150px_1fr]">
              <dt className="font-medium">{k}</dt>
              <dd className="text-muted">{v}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-medium">Evaluation</h2>
        <p className="text-muted">
          Measured on the τ²-bench retail test split: an LLM plays each customer from a scripted scenario, and a task
          passes only if the final database matches the expected one exactly and the agent said what it had to say.
          Reliability is reported as pass^k: the chance that k independent runs of a task all succeed.
        </p>
        {e.status === "pending" ? (
          <p className="text-sm text-muted">Results are being computed.</p>
        ) : (
          <pre className="overflow-x-auto rounded-lg border border-line bg-panel p-4 text-xs">{JSON.stringify(e, null, 2)}</pre>
        )}
      </section>
    </article>
  );
}
