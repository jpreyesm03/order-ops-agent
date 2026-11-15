import type { CSSProperties } from "react";
import Link from "next/link";
import Chat from "@/components/Chat";
import { HEADLINE } from "@/data/results";

const delay = (ms: number) => ({ "--d": `${ms}ms` }) as CSSProperties;

export default function Home() {
  return (
    <div>
      <section className="grid gap-10 pt-16 pb-14 sm:pt-24 lg:grid-cols-12">
        <div className="lg:col-span-8">
          <p className="eyebrow rise">Customer-service agent · live on a Shopify store</p>
          <h1 className="rise mt-6 font-serif text-5xl leading-[1.02] tracking-tight sm:text-7xl" style={delay(80)}>
            An agent that changes orders, <em className="text-accent">safely.</em>
          </h1>
        </div>
        <div
          className="rise flex flex-col justify-end gap-4 text-[15px] leading-relaxed text-muted lg:col-span-4"
          style={delay(160)}
        >
          <p>
            It cancels, modifies, returns and exchanges real orders. The rules that matter (who you are, what you own,
            an explicit &ldquo;yes&rdquo; before each change, no double refunds) are enforced in code, so prompt tricks
            don&apos;t get around them.
          </p>
          <p>
            Refunds over $250 wait for a person in the{" "}
            <Link
              href="/console"
              className="text-foreground underline decoration-line underline-offset-4 hover:decoration-foreground"
            >
              supervisor console
            </Link>
            .
          </p>
        </div>
      </section>

      <section className="grid grid-cols-2 border-y border-line lg:grid-cols-4">
        {HEADLINE.map((h, i) => (
          <div
            key={h.label}
            className={`rise py-7 pr-2 sm:px-6 ${i % 2 === 1 ? "border-l border-line pl-4" : ""} ${
              i >= 2 ? "border-t border-line lg:border-t-0" : ""
            } ${i === 2 ? "lg:border-l lg:pl-6" : ""}`}
            style={delay(240 + i * 60)}
          >
            <p className="font-serif text-4xl tabular-nums sm:text-5xl">{h.value}</p>
            <p className="mt-2 text-sm">{h.label}</p>
            <p className="mt-1 font-mono text-[11px] text-muted">{h.note}</p>
          </div>
        ))}
      </section>

      <section className="pt-16">
        <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="eyebrow">01 — Try it</p>
            <h2 className="mt-3 font-serif text-3xl sm:text-4xl">Chat as one of the demo customers</h2>
          </div>
          <p className="max-w-sm text-sm text-muted">
            Pick a customer, ask for a change, and watch every lookup and write the agent makes in the trace panel.
          </p>
        </div>
        <Chat />
      </section>
    </div>
  );
}
