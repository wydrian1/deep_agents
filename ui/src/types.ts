// Shapes of the Deep Agents HTTP API (see src/deep_agents_app/api).
import type { WorkflowSpec } from './lib/workflows';

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
  /** Set when this run was a step of a workflow. */
  workflow?: { id: string; name: string; node: string; node_label: string } | null;
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
  /** Only in the response of an edit: how many workflows that use this agent got a new version. */
  updated_workflows?: number;
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

// -- Knowledge base ---------------------------------------------------------------------------
export type MemorySummary = { key: string; revision: number; deleted: boolean; updated: string; writer: string; characters: number };
export type MemoryEntry = { key: string; revision: number; value: string; deleted: boolean; updated: string; writer: string };

// -- Workflows --------------------------------------------------------------------------------
export type WorkflowAgent = { name: string; prompt: string; mode: string; model: string | null; tools: string[]; files: string[] };
export type WorkflowSchedule = {
  schedule: Schedule; enabled: boolean; version: number; input: string; next_run: string | null; last_run_id: string | null; last_error: string;
};
export type WorkflowDefinition = {
  id: string; name: string; version: number; created: string; updated: string; spec: WorkflowSpec;
  agents: Record<string, WorkflowAgent>; schedule: WorkflowSchedule | null;
};
export type WorkflowRunStatus = 'running' | 'completed' | 'failed' | 'cancelled';
export type WorkflowRunSummary = {
  id: string; workflow_id: string; version: number; name: string; status: WorkflowRunStatus; outcome_status: Outcome;
  created: string; updated: string; error: string; heartbeat: string | null; rerun_of: string | null; scheduled_for: string | null;
};
export type WorkflowStepStatus = 'running' | 'completed' | 'failed' | 'skipped' | 'uncertain' | 'approval' | 'capacity' | 'launching';
export type WorkflowStep = {
  node_id: string; status: WorkflowStepStatus; child_id: string | null; decision: 'pending' | 'approved' | 'rejected';
  output: string; selected: string | null; updated: string; started: string | null; finished: string | null; metrics: RunMetrics | null;
};
export type WorkflowRunDetail = WorkflowRunSummary & {
  input: string; spec: WorkflowSpec; agents: Record<string, WorkflowAgent>; steps: WorkflowStep[]; can_continue: boolean; metrics: RunMetrics;
};
export type WorkflowOverview = {
  engine: string; definitions: WorkflowDefinition[]; runs: WorkflowRunSummary[];
  max_nodes: number; max_agent_nodes: number; max_active_workflows: number; max_stored_runs: number; max_agent_prompt: number; timeout_seconds: number;
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
