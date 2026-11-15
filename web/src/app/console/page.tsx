"use client";

import { useCallback, useEffect, useState } from "react";
import { StatusPill } from "@/components/Chat";
import { api, money, type Approval, type AuditRow, type Stats } from "@/lib/api";

const OUTCOME_CLS: Record<AuditRow["outcome"], string> = {
  ok: "text-faint",
  error: "text-warn",
  blocked: "text-bad",
  pending_approval: "text-warn",
  duplicate: "text-muted",
};

export default function Console() {
  const [pending, setPending] = useState<Approval[]>([]);
  const [history, setHistory] = useState<Approval[]>([]);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      Promise.all([api.approvals("pending"), api.approvals("all"), api.audit(60), api.stats()])
        .then(([p, all, a, s]) => {
          setPending(p);
          setHistory(all.filter((x) => x.status !== "pending").slice(0, 15));
          setAudit(a);
          setStats(s);
          setError(null);
        })
        .catch((e: Error) => setError(`Can't reach the API: ${e.message}`)),
    [],
  );

  useEffect(() => {
    const first = setTimeout(load, 0);
    const t = setInterval(load, 4000);
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [load]);

  async function decide(id: string, approve: boolean) {
    try {
      await api.decide(id, approve);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const blocked = stats ? Object.values(stats.blocked_by_rule).reduce((a, b) => a + b, 0) : 0;
  const tiles = stats
    ? [
        { label: "Conversations", value: stats.sessions },
        { label: "Tool calls that succeeded", value: stats.tool_outcomes.ok ?? 0 },
        { label: "Blocked by the gate", value: blocked },
        { label: "Waiting for you", value: pending.length, hot: pending.length > 0 },
      ]
    : [];

  return (
    <div>
      <header className="grid gap-8 pt-16 pb-12 sm:pt-24 lg:grid-cols-12">
        <div className="lg:col-span-8">
          <p className="eyebrow rise">02 — Supervisor console</p>
          <h1 className="rise mt-6 font-serif text-5xl leading-[1.02] tracking-tight sm:text-6xl">
            Big refunds wait for <em className="text-accent">a person.</em>
          </h1>
        </div>
        <p className="rise self-end text-[15px] leading-relaxed text-muted lg:col-span-4">
          Money movements above the limit stop here until someone decides. Approving runs the change exactly once.
          This page refreshes every few seconds.
        </p>
      </header>
      {error && <p className="mb-6 text-sm text-bad">{error}</p>}

      {stats && (
        <section className="grid grid-cols-2 border-y border-line lg:grid-cols-4">
          {tiles.map((t, i) => (
            <div
              key={t.label}
              className={`py-6 pr-2 sm:px-6 ${i % 2 === 1 ? "border-l border-line pl-4" : ""} ${
                i >= 2 ? "border-t border-line lg:border-t-0" : ""
              } ${i === 2 ? "lg:border-l lg:pl-6" : ""}`}
            >
              <p className={`font-serif text-4xl tabular-nums sm:text-5xl ${t.hot ? "text-warn" : ""}`}>{t.value}</p>
              <p className="eyebrow mt-2">{t.label}</p>
            </div>
          ))}
        </section>
      )}

      <section className="pt-12">
        <div className="mb-4 flex items-baseline justify-between">
          <p className="eyebrow">Approval inbox</p>
          <span className="font-mono text-xs text-faint">{pending.length} pending</span>
        </div>
        {pending.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-line px-6 py-10 text-center text-sm text-muted">
            Nothing waiting. Ask the agent for a refund over $250 in the chat to see one arrive.
          </div>
        ) : (
          <ul className="space-y-3">
            {pending.map((a) => (
              <li
                key={a.approval_id}
                className="flex flex-wrap items-center gap-4 rounded-2xl border border-line bg-panel px-5 py-4"
              >
                <div className="min-w-0 flex-1">
                  <p className="font-medium capitalize">
                    {a.tool.replaceAll("_", " ")} <span className="font-mono text-sm text-muted">{a.order_id}</span>
                  </p>
                  <p className="mt-1 font-mono text-[11px] text-muted">
                    requested {new Date(a.created_at).toLocaleTimeString()} · {a.approval_id}
                    {a.trace_url && (
                      <>
                        {" · "}
                        <a href={a.trace_url} target="_blank" rel="noreferrer" className="underline hover:text-foreground">
                          trace
                        </a>
                      </>
                    )}
                  </p>
                </div>
                <p className="font-serif text-3xl tabular-nums">{money(a.amount)}</p>
                <div className="flex gap-2">
                  <button
                    onClick={() => decide(a.approval_id, false)}
                    className="rounded-full border border-line px-4 py-1.5 text-sm transition-colors hover:border-foreground"
                  >
                    Reject
                  </button>
                  <button
                    onClick={() => decide(a.approval_id, true)}
                    className="rounded-full bg-foreground px-4 py-1.5 text-sm text-background transition-opacity hover:opacity-85"
                  >
                    Approve
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="grid gap-10 pt-14 lg:grid-cols-2">
        <section>
          <p className="eyebrow mb-4">Recent decisions</p>
          <ul className="divide-y divide-line border-y border-line text-sm">
            {history.length === 0 && <li className="py-4 text-muted">No decisions yet.</li>}
            {history.map((a) => (
              <li key={a.approval_id} className="flex items-center justify-between gap-3 py-3">
                <span className="truncate font-mono text-xs">{a.summary}</span>
                <span className="flex shrink-0 items-center gap-3">
                  {a.trace_url && (
                    <a href={a.trace_url} target="_blank" rel="noreferrer" className="text-xs text-muted underline">
                      trace
                    </a>
                  )}
                  <StatusPill status={a.status} />
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section>
          <p className="eyebrow mb-4">Audit log</p>
          <div className="scroll-quiet max-h-[440px] overflow-y-auto border-y border-line">
            <table className="w-full text-xs">
              <tbody className="divide-y divide-line">
                {audit.map((r) => (
                  <tr key={r.id}>
                    <td className="py-2 pr-4 font-mono text-faint">{new Date(r.ts).toLocaleTimeString()}</td>
                    <td className="py-2 font-mono">{r.tool}</td>
                    <td className={`py-2 pl-4 text-right ${OUTCOME_CLS[r.outcome]}`}>{r.outcome.replace("_", " ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}
