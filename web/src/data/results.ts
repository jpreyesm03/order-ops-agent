// Final results (release final-v1). Source of truth: README "Results" and DECISIONS.md D30–D36.
export const REPO_URL = "https://github.com/jpreyesm03/order-ops-agent";

export const HEADLINE = [
  { value: "86.5%", label: "Conversations resolved", note: "τ²-bench retail test tasks" },
  { value: "0.17%", label: "Wrong changes", note: "1 of 604 conversations" },
  { value: "0 / 52", label: "Attacks that got through", note: "policy gate on" },
  { value: "$0.07", label: "Per resolved conversation", note: "agent cost" },
];

export const BARS = [
  { id: "A1", name: "Resolution", target: "pass^1 ≥ 80%", result: "86.5%" },
  { id: "A2", name: "Consistency", target: "pass^4 ≥ 70%", result: "≈ 70%" },
  { id: "A3", name: "Wrong changes", target: "≤ 0.5% of conversations", result: "0.17%" },
  { id: "A4", name: "Safe failures", target: "≥ 90% change nothing wrong", result: "97.5%" },
  { id: "A5", name: "Attacks", target: "0 unauthorized writes", result: "0 of 52" },
  { id: "A6", name: "Cost", target: "≤ $0.50 per resolved conversation", result: "$0.069" },
];

export const BEFORE_AFTER = [
  { label: "Resolved (τ²-bench harness, 112 tasks × 4)", before: "77.5%", after: "86.6%" },
  { label: "Solved in all 4 trials", before: "52.7%", after: "72.3%" },
  { label: "Wrong changes", before: "~1.4%", after: "0.17%" },
];

export const HUMAN_CHECK = [
  { value: "0", label: "real agent errors the grader missed" },
  { value: "3 of 13", label: "grader failures that were the agent's fault" },
  { value: "1", label: "bug only a person caught (eval-mode approvals)" },
];
