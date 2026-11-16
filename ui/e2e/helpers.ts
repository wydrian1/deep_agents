import { expect, type APIRequestContext, type Page } from '@playwright/test';

export const unique = (prefix: string) => `${prefix}-${Math.random().toString(36).slice(2, 8)}`;

/** A saved test-mode agent (no gateway needed). */
export async function createAgent(request: APIRequestContext, name: string, prompt = 'Summarise the input.') {
  const response = await request.post('/api/agents', { data: { name, config: { prompt, mode: 'test', tools: [] } } });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()).id as string;
}

type Node = { id: string; kind: string; label: string; x: number; y: number; agentId: string; operator: string; value: string };
export const node = (id: string, kind: string, label: string, x: number, y: number, agentId = ''): Node => ({ id, kind, label, x, y, agentId, operator: '', value: '' });

/** Input -> Gate (approval) -> Worker (agent) -> Output, saved as version 1. */
export async function createApprovalWorkflow(request: APIRequestContext, name: string, agentId: string) {
  const spec = {
    version: 1,
    nodes: [node('start', 'start', 'Input', 200, 20), node('gate', 'approval', 'Gate', 200, 160), node('worker', 'agent', 'Worker', 200, 300, agentId), node('end', 'end', 'Output', 200, 440)],
    edges: [
      { source: 'start', target: 'gate', branch: 'always' },
      { source: 'gate', target: 'worker', branch: 'always' },
      { source: 'worker', target: 'end', branch: 'always' },
    ],
  };
  const response = await request.post('/api/workflows', { data: { name, expected_version: 0, spec } });
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()).id as string;
}

export async function openView(page: Page, name: string) {
  await page.goto('/');
  await page.getByRole('navigation', { name: 'Studio views' }).getByRole('button', { name }).click();
}
