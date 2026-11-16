// Pure figures for the daily summary (unit-tested); the component only lays them out.
export type DailySummary = {
  since: string;
  now: string;
  agents: { total: number; by_status: Record<string, number>; by_outcome: Record<string, number> };
  workflows: {
    total: number; by_status: Record<string, number>; by_outcome: Record<string, number>; problem_count: number;
    problems: { id: string; name: string; status: string; outcome: string; reason: string; updated: string }[];
  };
  tokens: { calls: number; input: number; output: number; cache: number; error_calls: number; cache_rate: number | null };
  limit: { rate_limited_calls: number; paused_until: string | null };
};

const sum = (values: Record<string, number>, ...keys: string[]) => keys.reduce((total, key) => total + (values[key] || 0), 0);

export function summaryFigures(summary: DailySummary) {
  const flows = summary.workflows;
  const agents = summary.agents;
  return {
    flowOk: Math.max(0, flows.total - flows.problem_count - sum(flows.by_status, 'running', 'cancelled')),
    flowStopped: flows.problem_count,
    flowRunning: sum(flows.by_status, 'running'),
    agentsOk: sum(agents.by_outcome, 'success', 'noop', 'unknown'),
    agentsStopped: sum(agents.by_outcome, 'blocked', 'error') + sum(agents.by_status, 'failed'),
    agentsRunning: sum(agents.by_status, 'running'),
    cachePercent: summary.tokens.cache_rate === null ? null : Math.round(summary.tokens.cache_rate * 1000) / 10,
  };
}
