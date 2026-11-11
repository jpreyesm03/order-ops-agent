export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8055";

export type ToolEvent = {
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  blocked: string | null;
  pending_approval: boolean;
  error: string | null;
};

export type Approval = {
  approval_id: string;
  tool: string;
  order_id: string | null;
  amount: number;
  summary: string;
  status: "pending" | "approved" | "rejected" | "failed";
  decided_by: string | null;
  decision_note: string | null;
  created_at: string;
  decided_at: string | null;
  session_id: string | null;
  trace_id: string | null;
  trace_url: string | null;
};

export type ChatReply = {
  reply: string;
  events: ToolEvent[];
  transferred: boolean;
  user_id: string | null;
  approvals: Approval[];
  latency_ms: number;
  cost_usd: number;
};

export type DemoCustomer = {
  user_id: string;
  first_name: string;
  last_name: string;
  email: string;
  zip: string;
  pending_order: string;
  delivered_order: string;
};

export type AuditRow = {
  id: number;
  ts: string;
  session_id: string;
  tool: string;
  tier: string;
  args: Record<string, unknown>;
  outcome: "ok" | "error" | "blocked" | "pending_approval" | "duplicate";
  detail: string | null;
};

export type Stats = {
  sessions: number;
  tool_outcomes: Record<string, number>;
  blocked_by_rule: Record<string, number>;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  createSession: () =>
    request<{ session_id: string; greeting: string; approval_threshold: number }>("/api/sessions", { method: "POST" }),
  send: (sessionId: string, text: string) =>
    request<ChatReply>(`/api/sessions/${sessionId}/messages`, { method: "POST", body: JSON.stringify({ text }) }),
  demoCustomers: () => request<DemoCustomer[]>("/api/demo/customers"),
  approvals: (status = "pending") => request<Approval[]>(`/api/approvals?status=${status}`),
  decide: (id: string, approve: boolean, note = "") =>
    request<{ status: string }>(`/api/approvals/${id}/decision`, {
      method: "POST",
      body: JSON.stringify({ approve, reviewer: "demo supervisor", note }),
    }),
  audit: (limit = 50) => request<AuditRow[]>(`/api/audit?limit=${limit}`),
  stats: () => request<Stats>("/api/stats"),
};

export const money = (n: number) => n.toLocaleString("en-US", { style: "currency", currency: "USD" });
