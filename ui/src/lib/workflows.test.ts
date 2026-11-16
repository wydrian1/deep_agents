import { describe, expect, it } from 'vitest';
import {
  connectWorkflow, diffVersions, duplicateLabel, editorProblem, FlowError, isConnected, lineChanges, nextNodeName, removeNode,
  type WorkflowNode, type WorkflowSpec, type WorkflowVersion,
} from './workflows';

const node = (id: string, kind: WorkflowNode['kind'], extra: Partial<WorkflowNode> = {}): WorkflowNode => ({
  id, kind, label: id, x: 0, y: 0, agentId: kind === 'agent' ? '11111111-1111-4111-8111-111111111111' : '', operator: '', value: '', ...extra,
});
const base = (): WorkflowSpec => ({ version: 1, nodes: [node('start', 'start'), node('a', 'agent'), node('end', 'end')], edges: [] });
const keyOf = (work: () => unknown) => {
  try { work(); } catch (reason) { return reason instanceof FlowError ? reason.key : 'other'; }
  return 'none';
};

describe('connectWorkflow', () => {
  it('adds a link and keeps the original spec untouched', () => {
    const spec = base();
    const next = connectWorkflow(spec, 'start', 'a', 'always');
    expect(next.edges).toEqual([{ source: 'start', target: 'a', branch: 'always' }]);
    expect(spec.edges).toEqual([]);
  });
  it('rejects the end as a source, the start as a target, self links and duplicates', () => {
    const spec = connectWorkflow(base(), 'start', 'a', 'always');
    expect(keyOf(() => connectWorkflow(spec, 'end', 'a', 'always'))).toBe('wf.err.fromEnd');
    expect(keyOf(() => connectWorkflow(spec, 'a', 'start', 'always'))).toBe('wf.err.toStart');
    expect(keyOf(() => connectWorkflow(spec, 'a', 'a', 'always'))).toBe('wf.err.self');
    expect(keyOf(() => connectWorkflow(spec, 'start', 'a', 'always'))).toBe('wf.err.duplicateEdge');
    expect(keyOf(() => connectWorkflow(spec, 'a', 'missing', 'always'))).toBe('wf.err.missingNode');
  });
  it('rejects cycles', () => {
    const spec: WorkflowSpec = { ...base(), nodes: [...base().nodes, node('b', 'agent')], edges: [{ source: 'a', target: 'b', branch: 'always' }] };
    expect(keyOf(() => connectWorkflow(spec, 'b', 'a', 'always'))).toBe('wf.err.cycle');
  });
  it('needs a free yes/no branch from a condition and allows branches only there', () => {
    const spec: WorkflowSpec = { version: 1, nodes: [node('start', 'start'), node('c', 'condition', { operator: 'empty' }), node('a', 'agent'), node('b', 'agent'), node('end', 'end')], edges: [] };
    expect(keyOf(() => connectWorkflow(spec, 'c', 'a', 'always'))).toBe('wf.err.pickBranch');
    const one = connectWorkflow(spec, 'c', 'a', 'true');
    expect(keyOf(() => connectWorkflow(one, 'c', 'b', 'true'))).toBe('wf.err.branchTaken');
    expect(connectWorkflow(one, 'c', 'b', 'false').edges).toHaveLength(2);
    expect(keyOf(() => connectWorkflow(spec, 'a', 'b', 'true'))).toBe('wf.err.branchOnlyCondition');
  });
  it('enforces the fan-in and fan-out limits', () => {
    const nodes = [node('start', 'start'), node('j', 'join'), node('a', 'agent'), node('b', 'agent'), node('c', 'agent'), node('d', 'agent'), node('e', 'agent'), node('end', 'end')];
    let spec: WorkflowSpec = { version: 1, nodes, edges: [] };
    for (const id of ['a', 'b', 'c', 'd']) spec = connectWorkflow(spec, id, 'j', 'always');
    expect(keyOf(() => connectWorkflow(spec, 'e', 'j', 'always'))).toBe('wf.err.edgeLimit');
    let fan: WorkflowSpec = { version: 1, nodes, edges: [] };
    for (const id of ['a', 'b', 'c']) fan = connectWorkflow(fan, 'start', id, 'always');
    expect(keyOf(() => connectWorkflow(fan, 'start', 'd', 'always'))).toBe('wf.err.edgeLimit');
  });
});

describe('editor helpers', () => {
  it('finds duplicate names case-insensitively and generates free names', () => {
    expect(duplicateLabel([{ label: 'Agent 1' }, { label: ' agent 1 ' }])).toBe(' agent 1 ');
    expect(duplicateLabel([{ label: 'a' }, { label: 'b' }])).toBeNull();
    expect(nextNodeName([node('agent_1', 'agent', { label: 'Agent 1' })], 'agent', 'Agent')).toEqual({ id: 'agent_2', label: 'Agent 2' });
  });
  it('removes a node together with its links', () => {
    const spec = connectWorkflow(connectWorkflow(base(), 'start', 'a', 'always'), 'a', 'end', 'always');
    const smaller = removeNode(spec, 'a');
    expect(smaller.nodes.map((item) => item.id)).toEqual(['start', 'end']);
    expect(smaller.edges).toEqual([]);
  });
  it('reports the first saving problem and accepts a complete graph', () => {
    const spec = base();
    expect(editorProblem('', spec)?.key).toBe('wf.problem.name');
    expect(editorProblem('x', spec)?.key).toBe('wf.problem.noOutgoing');
    const started = connectWorkflow(spec, 'start', 'a', 'always');
    expect(editorProblem('x', { ...started, nodes: started.nodes.map((item) => (item.id === 'a' ? { ...item, agentId: '' } : item)) })?.key).toBe('wf.problem.noAgent');
    const full = connectWorkflow(started, 'a', 'end', 'always');
    expect(editorProblem('x', full)).toBeNull();
    expect(editorProblem('x', { ...full, nodes: full.nodes.map((item) => (item.id === 'end' ? { ...item, label: 'Start' } : item.id === 'start' ? { ...item, label: 'start' } : item)) })?.key).toBe('wf.problem.duplicate');
  });
  it('requires both branches and a text for contains-conditions', () => {
    const spec: WorkflowSpec = { version: 1, nodes: [node('start', 'start'), node('c', 'condition', { operator: 'contains', value: '' }), node('a', 'agent'), node('end', 'end')], edges: [] };
    let flow = connectWorkflow(spec, 'start', 'c', 'always');
    expect(editorProblem('x', flow)?.key).toBe('wf.problem.noCondition');
    flow = { ...flow, nodes: flow.nodes.map((item) => (item.id === 'c' ? { ...item, value: 'ok' } : item)) };
    flow = connectWorkflow(connectWorkflow(connectWorkflow(flow, 'c', 'a', 'true'), 'a', 'end', 'always'), 'c', 'end', 'false');
    expect(editorProblem('x', flow)).toBeNull();
    expect(isConnected(flow)).toBe(true);
    expect(isConnected(base())).toBe(false);
  });
});

describe('version comparison', () => {
  const agent = { name: 'Writer', prompt: 'Line one\nLine two', mode: 'test', tools: ['web', 'files'] };
  const version = (n: number, spec: WorkflowSpec, agents = { '11111111-1111-4111-8111-111111111111': agent }): WorkflowVersion => ({ version: n, created: '2026-01-01T00:00:00Z', spec, agents });
  const spec = connectWorkflow(connectWorkflow(base(), 'start', 'a', 'always'), 'a', 'end', 'always');

  it('ignores pure layout moves', () => {
    const moved = { ...spec, nodes: spec.nodes.map((item) => ({ ...item, x: item.x + 50 })) };
    expect(diffVersions(version(1, spec), version(2, moved)).identical).toBe(true);
  });
  it('reports graph and agent changes, including prompt lines', () => {
    const changedSpec: WorkflowSpec = { ...spec, nodes: [...spec.nodes.map((item) => (item.id === 'a' ? { ...item, label: 'Renamed' } : item)), node('b', 'agent')] };
    const key = '11111111-1111-4111-8111-111111111111';
    const diff = diffVersions(version(1, spec), version(2, changedSpec, { [key]: { ...agent, prompt: 'Line one\nLine three', tools: ['files'] } }));
    expect(diff.nodes.added).toEqual(['b']);
    expect(diff.nodes.changed).toEqual([{ id: 'a', label: 'Renamed', fields: ['label'] }]);
    expect(diff.agents.changed[0].fields).toEqual(['tools', 'prompt']);
    expect(diff.agents.changed[0].promptLines).toEqual({ added: ['Line three'], removed: ['Line two'] });
    expect(diff.identical).toBe(false);
  });
  it('names links by their step labels', () => {
    const without = { ...spec, edges: [spec.edges[0]] };
    expect(diffVersions(version(1, spec), version(2, without)).edges.removed).toEqual(['a → end']);
  });
  it('compares prompt lines without order or whitespace noise', () => {
    expect(lineChanges('a\n b ', 'b\na')).toEqual({ added: [], removed: [] });
  });
});
