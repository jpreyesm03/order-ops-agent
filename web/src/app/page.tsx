import Link from "next/link";
import Chat from "@/components/Chat";

export default function Home() {
  return (
    <div className="space-y-6">
      <section className="max-w-3xl space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">An agent that changes orders, safely</h1>
        <p className="text-muted">
          Chat as one of the demo customers below. The agent can cancel, modify, return and exchange orders. The
          rules that matter (who you are, what you own, an explicit &ldquo;yes&rdquo; before each change, no double
          refunds) are enforced in code, so prompt tricks don&apos;t bypass them. Refunds over the limit wait for a
          supervisor in the <Link href="/console" className="underline">console</Link>.
        </p>
      </section>
      <Chat />
    </div>
  );
}
