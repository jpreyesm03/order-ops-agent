"use client";

import { useCallback, useEffect, useState } from "react";
import { StatusPill } from "@/components/Chat";
import { api, money, type Approval, type AuditRow, type Stats } from "@/lib/api";

const OUTCOME_CLS: Record<AuditRow["outcome"], string> = {
  ok: "text-ok",
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

  return (
    <div className="space-y-6">
      <section className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Supervisor console</h1>
        <p className="text-muted">
          Money movements above the limit stop here until a person decides. Approving runs the change exactly once.
        </p>
      </section>
      {error && <p className="text-sm text-bad">{error}</p>}

      {stats && (
        <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Tile label="Conversations" value={stats.sessions} />
          <Tile label="Changes applied" value={stats.tool_outcomes.ok ?? 0} hint="all tool calls that succeeded" />
          <Tile label="Blocked by the gate" value={blocked} />
          <Tile label="Waiting for you" value={pending.length} />
        </section>
      )}

      <section className="rounded-lg border border-line bg-panel">
        <h2 className="border-b border-line px-4 py-3 font-medium">Approval inbox</h2>
        {pending.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted">Nothing waiting. Ask the agent for a large refund in the chat to see one arrive.</p>
        ) : (
          <ul className="divide-y divide-line">
            {pending.map((a) => (
              <li key={a.approval_id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
                <div className="min-w-0 flex-1">
                  <p className="font-medium">
                    {a.tool.replaceAll("_", " ")} · {a.order_id}
                  </p>
                  <p className="text-xs text-muted">
                    {money(a.amount)} · requested {new Date(a.created_at).toLocaleTimeString()} · {a.approval_id}
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
                <button onClick={() => decide(a.approval_id, false)} className="rounded border border-line px-3 py-1 hover:bg-background">
                  Reject
                </button>
                <button onClick={() => decide(a.approval_id, true)} className="rounded bg-accent px-3 py-1 text-background">
                  Approve
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="rounded-lg border border-line bg-panel">
          <h2 className="border-b border-line px-4 py-3 font-medium">Recent decisions</h2>
          <ul className="divide-y divide-line text-sm">
            {history.length === 0 && <li className="px-4 py-4 text-muted">No decisions yet.</li>}
            {history.map((a) => (
              <li key={a.approval_id} className="flex items-center justify-between gap-2 px-4 py-2">
                <span className="truncate font-mono text-xs">{a.summary}</span>
                <span className="flex items-center gap-2">
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

        <section className="rounded-lg border border-line bg-panel">
          <h2 className="border-b border-line px-4 py-3 font-medium">Audit log</h2>
          <div className="max-h-[420px] overflow-y-auto">
            <table className="w-full text-xs">
              <tbody className="divide-y divide-line">
                {audit.map((r) => (
                  <tr key={r.id}>
                    <td className="px-4 py-1.5 text-muted">{new Date(r.ts).toLocaleTimeString()}</td>
                    <td className="py-1.5 font-mono">{r.tool}</td>
                    <td className={`px-4 py-1.5 ${OUTCOME_CLS[r.outcome]}`}>{r.outcome.replace("_", " ")}</td>
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

function Tile({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel p-3" title={hint}>
      <p className="text-xs text-muted">{label}</p>
      <p className="text-2xl font-semibold tabular-nums">{value}</p>
    </div>
  );
}
