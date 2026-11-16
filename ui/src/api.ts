import type { WorkflowSpec, WorkflowVersion } from './lib/workflows';
import type {
  Agent, MemoryEntry, MemorySummary, RunMetricsDetail, Run, Schedule, ServiceStatus, StudioGraph, ToolInfo, UploadedFile,
  WorkflowOverview, WorkflowRunDetail,
} from './types';

export class ApiError extends Error {
  status: number;
  /** Structured details of a conflict (e.g. the knowledge base's current revision). */
  current_revision: number | null;
  constructor(message: string, status: number, currentRevision: number | null = null) {
    super(message);
    this.status = status;
    this.current_revision = currentRevision;
  }
}

/** FastAPI returns ``{detail: string}`` or, for validation errors, ``{detail: [{msg, loc}]}``. */
function detailOf(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && !Array.isArray(detail) && typeof (detail as { message?: unknown }).message === 'string') return (detail as { message: string }).message;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => (item && typeof item === 'object' && 'msg' in item ? String((item as { msg: unknown }).msg) : '')).filter(Boolean);
    if (parts.length) return parts.join('; ');
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit, fallback = 'Request failed'): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { cache: 'no-store', ...init });
  } catch {
    throw new ApiError(fallback, 0);
  }
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const revision = (body as { detail?: { current_revision?: unknown } } | null)?.detail?.current_revision;
    throw new ApiError(detailOf(body, fallback), response.status, typeof revision === 'number' ? revision : null);
  }
  return body as T;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export type RunInput = { prompt: string; model?: string | null; mode: 'live' | 'test'; tools: string[]; files: string[]; language?: string };
export type ScheduleInput = { schedule: Schedule | null; enabled: boolean; confirm_recurring_cost: boolean };

export const api = {
  runs: () => request<Run[]>('/api/runs'),
  run: (id: string) => request<Run>('/api/runs/' + id),
  agents: () => request<Agent[]>('/api/agents'),
  status: () => request<ServiceStatus>('/api/status'),
  tools: () => request<ToolInfo[]>('/api/tools'),
  files: () => request<UploadedFile[]>('/api/files'),
  metrics: (id: string, signal?: AbortSignal) => request<RunMetricsDetail>(`/api/runs/${id}/metrics`, { signal }),
  graph: (id: string, after: number, signal?: AbortSignal) => request<StudioGraph>(`/api/langgraph/threads/${id}/graph?after=${after}`, { signal }),
  me: () => request<{ id: string; email: string; name: string }>('/auth/me'),

  startRun: (input: RunInput) => request<Run>('/api/runs', json('POST', input)),
  rerun: (id: string) => request<Run>(`/api/runs/${id}/rerun`, json('POST')),
  cancel: (id: string) => request<{ cancelled: boolean }>(`/api/runs/${id}/cancel`, json('POST')),
  upload: (file: File) =>
    request<UploadedFile>('/api/files?name=' + encodeURIComponent(file.name), { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file }),

  createAgent: (name: string, config: RunInput, schedule: ScheduleInput) =>
    request<Agent>('/api/agents', json('POST', { name, config, ...schedule })),
  updateAgent: (id: string, revision: number, name: string, config: RunInput, schedule: ScheduleInput) =>
    request<Agent>('/api/agents/' + id, json('PUT', { name, config, revision, ...schedule })),
  agentFromRun: (runId: string, name: string, schedule: ScheduleInput) =>
    request<Agent>('/api/agents/from-run/' + runId, json('POST', { name, ...schedule })),
  enableAgent: (id: string, enabled: boolean) =>
    request<Agent>(`/api/agents/${id}/enable`, json('POST', { enabled, confirm_recurring_cost: enabled })),
  deleteAgent: (id: string) => request<void>('/api/agents/' + id, json('DELETE')),
  runAgent: (id: string) => request<Run>(`/api/agents/${id}/run`, json('POST')),
  logout: () => request<void>('/auth/logout', json('POST')),

  // Knowledge base
  memoryList: (prefix: string, deleted: boolean, after = '') =>
    request<{ entries: MemorySummary[]; next: string | null }>(`/api/memory?prefix=${encodeURIComponent(prefix)}&deleted=${deleted}&after=${encodeURIComponent(after)}`),
  memoryGet: (key: string, revision = 0) => request<{ entry: MemoryEntry | null }>(`/api/memory/${encodeURIComponent(key)}?revision=${revision}`),
  memoryHistory: (key: string, before?: number) =>
    request<{ versions: MemoryEntry[]; next: number | null }>(`/api/memory/${encodeURIComponent(key)}/history${before ? `?before=${before}` : ''}`),
  memoryPut: (key: string, value: string, expectedRevision: number, requestId: string) =>
    request<{ entry: MemoryEntry }>(`/api/memory/${encodeURIComponent(key)}`, json('PUT', { value, expected_revision: expectedRevision, request_id: requestId })),
  memoryDelete: (key: string, expectedRevision: number, requestId: string) =>
    request<unknown>(`/api/memory/${encodeURIComponent(key)}/delete`, json('POST', { expected_revision: expectedRevision, request_id: requestId })),

  // Workflows
  workflows: () => request<WorkflowOverview>('/api/workflows'),
  workflowRun: (id: string) => request<WorkflowRunDetail>('/api/workflow-runs/' + id),
  workflowVersions: (id: string) => request<{ versions: WorkflowVersion[] }>(`/api/workflows/${id}/versions`),
  saveWorkflow: (input: { id?: string; name: string; expected_version: number; spec: WorkflowSpec }) =>
    request<{ id: string; version: number }>('/api/workflows', json('POST', input)),
  deleteWorkflow: (id: string) => request<void>('/api/workflows/' + id, json('DELETE')),
  startWorkflow: (id: string, version: number, input: string, acceptedDataFlow: boolean) =>
    request<{ id: string }>(`/api/workflows/${id}/start`, json('POST', { version, input, accepted_data_flow: acceptedDataFlow })),
  scheduleWorkflow: (id: string, body: { schedule: Schedule; version: number; input: string; accepted_schedule: boolean; accepted_data_flow: boolean }) =>
    request<{ ok: boolean; next_run: string }>(`/api/workflows/${id}/schedule`, json('PUT', body)),
  removeWorkflowSchedule: (id: string) => request<void>(`/api/workflows/${id}/schedule`, json('DELETE')),
  pauseWorkflowSchedule: (id: string) => request<{ ok: boolean }>(`/api/workflows/${id}/schedule/pause`, json('POST')),
  rerunWorkflow: (id: string, acceptedDataFlow: boolean) => request<{ id: string }>(`/api/workflow-runs/${id}/rerun`, json('POST', { accepted_data_flow: acceptedDataFlow })),
  cancelWorkflow: (id: string) => request<{ ok: boolean }>(`/api/workflow-runs/${id}/cancel`, json('POST')),
  continueWorkflow: (id: string) => request<{ ok: boolean }>(`/api/workflow-runs/${id}/continue`, json('POST')),
  approveWorkflow: (id: string, nodeId: string, approved: boolean) =>
    request<{ ok: boolean }>(`/api/workflow-runs/${id}/approve`, json('POST', { node_id: nodeId, approved })),
};

/** Download URL of a private run output. */
export const artifactUrl = (runId: string, artifactId: string) => `/api/runs/${runId}/artifacts/${artifactId}`;
