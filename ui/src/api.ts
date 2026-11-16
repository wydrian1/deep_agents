import type { Agent, RunMetricsDetail, Run, Schedule, ServiceStatus, StudioGraph, ToolInfo, UploadedFile } from './types';

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/** FastAPI returns ``{detail: string}`` or, for validation errors, ``{detail: [{msg, loc}]}``. */
function detailOf(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string') return detail;
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
  if (!response.ok) throw new ApiError(detailOf(body, fallback), response.status);
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
};

/** Download URL of a private run output. */
export const artifactUrl = (runId: string, artifactId: string) => `/api/runs/${runId}/artifacts/${artifactId}`;
