// Shapes of the Deep Agents HTTP API (see src/deep_agents_app/api).
export type RunStatus = 'running' | 'completed' | 'failed' | 'cancelled';
export type Outcome = 'unknown' | 'success' | 'noop' | 'blocked' | 'error';

export type RunMetrics = {
  wall_ms: number;
  startup_ms: number | null;
  model_ms: number | null;
  tool_ms: number;
  other_ms: number | null;
  model_calls: number;
  subagent_calls: number;
  error_calls: number;
  tool_calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  reasoning_tokens: number | null;
  usage_calls: number;
  measured: boolean;
};

export type CallMetric = {
  seq: number;
  offset_ms: number;
  duration_ms: number;
  scope: 'main' | 'subagent';
  node: string;
  status: 'ok' | 'error';
  error: string;
  http_status: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  reasoning_tokens: number | null;
  model: string;
};
export type ToolMetric = { tool: string; operation: string; outcome: string; offset_ms: number; duration_ms: number };
export type RunMetricsDetail = { summary: RunMetrics; calls: CallMetric[]; tools: ToolMetric[]; truncated: boolean };

export type Artifact = { id: string; name: string; title: string; size: number };

export type Run = {
  id: string;
  status: RunStatus;
  mode: 'live' | 'test';
  model: string;
  prompt: string;
  tools: string[];
  steps: number;
  error: string;
  agent_id: string | null;
  rerun_of: string | null;
  scheduled_for: string | null;
  created: string;
  updated: string;
  outcome_status: Outcome;
  metrics?: RunMetrics | null;
  tool_calls?: number;
  artifacts?: Artifact[];
  // Only in the detail view:
  result?: string;
  language?: string;
  files?: string[];
};

export type Schedule = { kind: 'hourly' | 'daily' | 'weekly'; minute: number; hour?: number; weekday?: number };
export type AgentConfig = { prompt: string; model: string | null; mode: 'live' | 'test'; language: string; tools: string[]; files: string[] };
export type Agent = {
  id: string;
  name: string;
  config: AgentConfig;
  schedule: Schedule | null;
  enabled: boolean;
  next_run: string | null;
  last_run_id: string | null;
  last_error: string;
  revision: number;
  created: string;
  updated: string;
};

export type ToolInfo = { name: string; label: string; access: string; description: string; requires: string[] };
export type UploadedFile = { id: string; name: string; size: number };

export type ServiceStatus = {
  model: string | null;
  language: string;
  max_concurrent_runs: number;
  run_timeout_seconds: number;
  max_prompt_chars: number;
  active_runs: number;
  auth_enabled: boolean;
  scheduler_enabled: boolean;
  schedule_timezone: string;
  scheduler_heartbeat: string | null;
  limit_paused_until: string | null;
};

export type StudioNode = { id: string };
export type StudioEdge = { source: string; target: string; conditional?: boolean };
export type StudioEvent = {
  id: number;
  node: string;
  namespace: string;
  kind: 'message' | 'update' | 'interrupt';
  step: number | null;
  summary: string;
  detail: string;
};
export type StudioGraph = {
  nodes: StudioNode[];
  edges: StudioEdge[];
  coverage: 'definition' | 'observed';
  events: StudioEvent[];
  cursor: number;
  hasMore: boolean;
};
