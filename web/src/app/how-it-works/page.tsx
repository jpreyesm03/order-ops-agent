import { BARS, BEFORE_AFTER, HUMAN_CHECK, REPO_URL } from "@/data/results";

const RULES = [
  ["Identity", "Account data and every change require the customer to be found by email, or by name + zip code."],
  ["One customer", "After verification, lookups that resolve to someone else are blocked for the rest of the chat."],
  ["Ownership", "Orders, profiles and payment methods must belong to the verified customer."],
  ["Explicit yes", "A change runs only if the customer's latest message is an explicit confirmation. One yes, one change."],
  ["Provenance", "Addresses, payment methods and item ids in a change must come from the customer or from this conversation's data."],
  ["Human approval", "Money movements above $250 are queued for a supervisor instead of executed."],
  ["Idempotency", "The same change with the same arguments in one conversation runs once. No double refunds."],
  ["Audit log", "Every tool call is recorded with its outcome: ok, error, blocked, pending approval or duplicate."],
];

const FLOW = [
  { k: "Customer", v: "chat or phone" },
  { k: "Agent", v: "Claude, one tool call at a time" },
  { k: "Policy gate", v: "rules in code, before any write", strong: true },
  { k: "Store", v: "Shopify + Postgres mirror" },
];

function Section({ n, title, children }: { n: string; title: string; children: React.ReactNode }) {
  return (
    <section className="grid gap-6 border-t border-line py-14 lg:grid-cols-12">
      <div className="lg:col-span-4">
        <p className="eyebrow">{n}</p>
        <h2 className="mt-3 font-serif text-3xl sm:text-4xl">{title}</h2>
      </div>
      <div className="lg:col-span-8">{children}</div>
    </section>
  );
}

export default function HowItWorks() {
  return (
    <article>
      <header className="grid gap-8 pt-16 pb-14 sm:pt-24 lg:grid-cols-12">
        <div className="lg:col-span-8">
          <p className="eyebrow rise">03 — How it works</p>
          <h1 className="rise mt-6 font-serif text-5xl leading-[1.02] tracking-tight sm:text-6xl">
            The model proposes. <em className="text-accent">Code decides.</em>
          </h1>
        </div>
        <p className="rise self-end text-[15px] leading-relaxed text-muted lg:col-span-4">
          Most demo agents only read data. This one changes it, so the real question isn&apos;t &ldquo;is the model
          smart&rdquo; but &ldquo;what stops it from doing damage when it&apos;s wrong or manipulated&rdquo;. Here, the
          rules live in code around the tools, not only in the prompt.
        </p>
      </header>

      <Section n="Architecture" title="One path for every change">
        <ol className="grid gap-px overflow-hidden rounded-2xl border border-line bg-line sm:grid-cols-4">
          {FLOW.map((f, i) => (
            <li key={f.k} className={`relative p-5 ${f.strong ? "bg-accent-soft" : "bg-panel"}`}>
              <span className="font-mono text-[10px] text-faint">0{i + 1}</span>
              <p className={`mt-6 font-serif text-2xl ${f.strong ? "text-accent" : ""}`}>{f.k}</p>
              <p className="mt-1 text-xs text-muted">{f.v}</p>
              {i < FLOW.length - 1 && (
                <span className="absolute top-5 right-4 hidden text-faint sm:block" aria-hidden>
                  →
                </span>
              )}
            </li>
          ))}
        </ol>
        <p className="mt-4 text-sm text-muted">
          Refunds over the limit leave the path at the gate and wait in the supervisor console; approving runs the
          change exactly once. Every step is traced with OpenTelemetry.
        </p>
      </Section>

      <Section n="Guardrails" title="The rules enforced in code">
        <dl className="grid gap-px overflow-hidden rounded-2xl border border-line bg-line sm:grid-cols-2">
          {RULES.map(([k, v], i) => (
            <div key={k} className="bg-panel p-5">
              <dt className="flex items-baseline gap-3">
                <span className="font-mono text-[10px] text-faint">{String(i + 1).padStart(2, "0")}</span>
                <span className="font-medium">{k}</span>
              </dt>
              <dd className="mt-2 text-sm leading-relaxed text-muted">{v}</dd>
            </div>
          ))}
        </dl>
      </Section>

      <Section n="Evaluation" title="Six bars, set before the final run">
        <p className="max-w-2xl text-[15px] leading-relaxed text-muted">
          Measured on Sierra&apos;s τ²-bench retail tasks: an LLM plays each customer from a scripted scenario, and a
          conversation passes only if the final database matches the expected one exactly and the agent said what it had
          to say. The bars were written down before any final run.
        </p>
        <div className="mt-8 divide-y divide-line border-y border-line">
          {BARS.map((b) => (
            <div key={b.id} className="grid grid-cols-[40px_1fr_auto] items-baseline gap-4 py-4 sm:grid-cols-[48px_1fr_1fr_auto]">
              <span className="font-mono text-xs text-faint">{b.id}</span>
              <span className="font-medium">{b.name}</span>
              <span className="hidden text-sm text-muted sm:block">{b.target}</span>
              <span className="flex items-baseline gap-3">
                <span className="font-serif text-2xl tabular-nums">{b.result}</span>
                <span className="text-xs text-ok">met</span>
              </span>
            </div>
          ))}
        </div>

        <p className="eyebrow mt-12 mb-4">Before → after (stronger model + code checks)</p>
        <div className="divide-y divide-line border-y border-line">
          {BEFORE_AFTER.map((r) => (
            <div key={r.label} className="grid grid-cols-[1fr_auto] items-baseline gap-4 py-3 text-sm">
              <span className="text-muted">{r.label}</span>
              <span className="font-mono tabular-nums">
                <span className="text-faint">{r.before}</span> <span className="text-faint">→</span>{" "}
                <span className="text-foreground">{r.after}</span>
              </span>
            </div>
          ))}
        </div>
      </Section>

      <Section n="Checking the grader" title="I labeled 40 conversations myself">
        <p className="max-w-2xl text-[15px] leading-relaxed text-muted">
          Blind to the automatic grader and the LLM reviewer. The grader is strict: it fails the agent whenever the
          simulated customer strays from its script, so the bars above are a conservative measure.
        </p>
        <div className="mt-8 grid gap-px overflow-hidden rounded-2xl border border-line bg-line sm:grid-cols-3">
          {HUMAN_CHECK.map((h) => (
            <div key={h.label} className="bg-panel p-5">
              <p className="font-serif text-4xl tabular-nums">{h.value}</p>
              <p className="mt-2 text-sm text-muted">{h.label}</p>
            </div>
          ))}
        </div>
        <a href={`${REPO_URL}#results`} target="_blank" rel="noreferrer" className="link-grow mt-8 inline-block text-sm">
          Full results, decisions and code on GitHub ↗
        </a>
      </Section>
    </article>
  );
}
