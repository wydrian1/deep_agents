// Workflow graph model and the pure editing rules (the server validates every save again).
export type NodeKind = 'start' | 'agent' | 'condition' | 'join' | 'approval' | 'end';
export type Branch = 'always' | 'true' | 'false';
export type Operator = '' | 'contains' | 'not_contains' | 'empty' | 'nonempty';
export type WorkflowNode = { id: string; kind: NodeKind; label: string; x: number; y: number; agentId: string; operator: Operator; value: string };
export type WorkflowEdge = { source: string; target: string; branch: Branch };
export type WorkflowSpec = { version: 1; nodes: WorkflowNode[]; edges: WorkflowEdge[] };

export const MAX_NODES = 12;
export const MAX_AGENT_NODES = 8;
export const MAX_EDGES = 24;
export const NODE_W = 180;
export const NODE_H = 104;

/** A rule violation: an i18n key plus values; the UI translates it. */
export class FlowError extends Error {
  key: string;
  values: Record<string, string | number>;
  constructor(key: string, values: Record<string, string | number> = {}) {
    super(key);
    this.key = key;
    this.values = values;
  }
}

export const starterSpec = (): WorkflowSpec => ({
  version: 1,
  nodes: [
    { id: 'start', kind: 'start', label: 'Input', x: 40, y: 40, agentId: '', operator: '', value: '' },
    { id: 'end', kind: 'end', label: 'Output', x: 40, y: 280, agentId: '', operator: '', value: '' },
  ],
  edges: [],
});

/** The first label used twice (case-insensitive, trimmed), or null. */
export function duplicateLabel(nodes: Pick<WorkflowNode, 'label'>[]): string | null {
  const seen = new Set<string>();
  for (const node of nodes) {
    const key = node.label.trim().toLocaleLowerCase();
    if (seen.has(key)) return node.label;
    seen.add(key);
  }
  return null;
}

/** A free name such as "Agent 2" and a free technical id such as "agent_2". */
export function nextNodeName(nodes: WorkflowNode[], kind: NodeKind, base: string): { id: string; label: string } {
  const ids = new Set(nodes.map((node) => node.id));
  const labels = new Set(nodes.map((node) => node.label.trim().toLocaleLowerCase()));
  for (let index = 1; ; index += 1) {
    const id = `${kind}_${index}`;
    const label = `${base} ${index}`;
    if (!ids.has(id) && !labels.has(label.toLocaleLowerCase())) return { id, label };
  }
}

/** Adding an edge must never reverse the terminal nodes or introduce a cycle. */
export function connectWorkflow(spec: WorkflowSpec, sourceId: string, targetId: string, branch: Branch): WorkflowSpec {
  const source = spec.nodes.find((node) => node.id === sourceId);
  const target = spec.nodes.find((node) => node.id === targetId);
  if (!source || !target) throw new FlowError('wf.err.missingNode');
  if (source.kind === 'end') throw new FlowError('wf.err.fromEnd');
  if (target.kind === 'start') throw new FlowError('wf.err.toStart');
  if (sourceId === targetId) throw new FlowError('wf.err.self');
  if (spec.edges.some((edge) => edge.source === sourceId && edge.target === targetId)) throw new FlowError('wf.err.duplicateEdge');
  if (spec.edges.length >= MAX_EDGES || spec.edges.filter((edge) => edge.source === sourceId).length >= 3 || spec.edges.filter((edge) => edge.target === targetId).length >= 4) {
    throw new FlowError('wf.err.edgeLimit');
  }
  if (source.kind === 'condition') {
    if (branch !== 'true' && branch !== 'false') throw new FlowError('wf.err.pickBranch');
    if (spec.edges.some((edge) => edge.source === sourceId && edge.branch === branch)) throw new FlowError('wf.err.branchTaken');
  } else if (branch !== 'always') {
    throw new FlowError('wf.err.branchOnlyCondition');
  }
  const pending = [targetId];
  const visited = new Set<string>();
  while (pending.length) {
    const id = pending.pop() as string;
    if (id === sourceId) throw new FlowError('wf.err.cycle');
    if (visited.has(id)) continue;
    visited.add(id);
    for (const edge of spec.edges) if (edge.source === id) pending.push(edge.target);
  }
  return { ...spec, edges: [...spec.edges, { source: sourceId, target: targetId, branch }] };
}

export function removeNode(spec: WorkflowSpec, id: string): WorkflowSpec {
  return { ...spec, nodes: spec.nodes.filter((node) => node.id !== id), edges: spec.edges.filter((edge) => edge.source !== id && edge.target !== id) };
}

/** The first thing that blocks saving, as an i18n key (null = the graph looks saveable). */
export function editorProblem(name: string, spec: WorkflowSpec): { key: string; values?: Record<string, string | number> } | null {
  if (!name.trim()) return { key: 'wf.problem.name' };
  if (name.length > 100) return { key: 'wf.problem.nameLong' };
  const duplicate = duplicateLabel(spec.nodes);
  if (duplicate !== null) return { key: 'wf.problem.duplicate', values: { label: duplicate } };
  if (spec.nodes.length > MAX_NODES) return { key: 'wf.problem.tooManyNodes' };
  if (spec.nodes.filter((node) => node.kind === 'agent').length > MAX_AGENT_NODES) return { key: 'wf.problem.tooManyAgents' };
  for (const node of spec.nodes) {
    if (node.kind === 'agent' && !node.agentId) return { key: 'wf.problem.noAgent', values: { label: node.label } };
    if (node.kind === 'condition' && (node.operator === '' || ((node.operator === 'contains' || node.operator === 'not_contains') && !node.value.trim()))) {
      return { key: 'wf.problem.noCondition', values: { label: node.label } };
    }
    if (node.kind !== 'start' && !spec.edges.some((edge) => edge.target === node.id)) return { key: 'wf.problem.noIncoming', values: { label: node.label } };
    if (node.kind !== 'end' && !spec.edges.some((edge) => edge.source === node.id)) return { key: 'wf.problem.noOutgoing', values: { label: node.label } };
    if (node.kind === 'condition') {
      const branches = spec.edges.filter((edge) => edge.source === node.id).map((edge) => edge.branch);
      if (!branches.includes('true') || !branches.includes('false')) return { key: 'wf.problem.branches', values: { label: node.label } };
    }
  }
  return null;
}

/** Everything reachable from start also reaches the end (the server's connectivity rule), for the live hint. */
export function isConnected(spec: WorkflowSpec): boolean {
  const start = spec.nodes.find((node) => node.kind === 'start');
  const end = spec.nodes.find((node) => node.kind === 'end');
  if (!start || !end) return false;
  const walk = (from: string, forward: boolean) => {
    const seen = new Set([from]);
    const stack = [from];
    while (stack.length) {
      const id = stack.pop() as string;
      for (const edge of spec.edges) {
        const next = forward ? (edge.source === id ? edge.target : null) : edge.target === id ? edge.source : null;
        if (next && !seen.has(next)) {
          seen.add(next);
          stack.push(next);
        }
      }
    }
    return seen;
  };
  const forward = walk(start.id, true);
  const backward = walk(end.id, false);
  return spec.nodes.every((node) => forward.has(node.id) && backward.has(node.id));
}

// -- Version comparison (pure data; nothing is restored) ------------------------------------------

export type VersionAgent = { name: string; prompt: string; mode: string; tools: string[] };
export type WorkflowVersion = { version: number; created: string; spec: WorkflowSpec; agents: Record<string, VersionAgent> };
export type VersionDiff = {
  nodes: { added: string[]; removed: string[]; changed: { id: string; label: string; fields: string[] }[] };
  edges: { added: string[]; removed: string[] };
  agents: { added: string[]; removed: string[]; changed: { key: string; name: string; fields: string[]; promptLines?: { added: string[]; removed: string[] } }[] };
  identical: boolean;
};
const NODE_FIELDS = ['kind', 'label', 'agentId', 'operator', 'value'] as const;
const edgeKey = (edge: WorkflowEdge) => `${edge.source} → ${edge.target}${edge.branch === 'always' ? '' : ` (${edge.branch})`}`;
const lines = (text: string) => text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);

export function lineChanges(before: string, after: string) {
  const old = lines(before);
  const next = lines(after);
  const oldSet = new Set(old);
  const nextSet = new Set(next);
  return { added: next.filter((line) => !oldSet.has(line)), removed: old.filter((line) => !nextSet.has(line)) };
}

/** Field names are returned as i18n-free codes ("kind", "label", ...); the view translates them. */
export function diffVersions(from: WorkflowVersion, to: WorkflowVersion): VersionDiff {
  const fromNodes = new Map(from.spec.nodes.map((node) => [node.id, node]));
  const toNodes = new Map(to.spec.nodes.map((node) => [node.id, node]));
  const nodes: VersionDiff['nodes'] = {
    added: [...toNodes.values()].filter((node) => !fromNodes.has(node.id)).map((node) => node.label),
    removed: [...fromNodes.values()].filter((node) => !toNodes.has(node.id)).map((node) => node.label),
    changed: [],
  };
  for (const [id, after] of toNodes) {
    const before = fromNodes.get(id);
    if (!before) continue; // x/y moves are deliberately not a change
    const fields = NODE_FIELDS.filter((field) => before[field] !== after[field]);
    if (fields.length) nodes.changed.push({ id, label: after.label, fields: [...fields] });
  }
  const labelOf = (spec: WorkflowSpec, id: string) => spec.nodes.find((node) => node.id === id)?.label ?? id;
  const named = (spec: WorkflowSpec, edge: WorkflowEdge) => `${labelOf(spec, edge.source)} → ${labelOf(spec, edge.target)}${edge.branch === 'always' ? '' : ` (${edge.branch})`}`;
  const fromEdges = new Map(from.spec.edges.map((edge) => [edgeKey(edge), edge]));
  const toEdges = new Map(to.spec.edges.map((edge) => [edgeKey(edge), edge]));
  const edges = {
    added: [...toEdges].filter(([key]) => !fromEdges.has(key)).map(([, edge]) => named(to.spec, edge)),
    removed: [...fromEdges].filter(([key]) => !toEdges.has(key)).map(([, edge]) => named(from.spec, edge)),
  };
  const agents: VersionDiff['agents'] = {
    added: Object.keys(to.agents).filter((key) => !(key in from.agents)).map((key) => to.agents[key].name),
    removed: Object.keys(from.agents).filter((key) => !(key in to.agents)).map((key) => from.agents[key].name),
    changed: [],
  };
  for (const key of Object.keys(to.agents)) {
    const before = from.agents[key];
    const after = to.agents[key];
    if (!before) continue;
    const fields: string[] = [];
    if (before.name !== after.name) fields.push('name');
    if (before.mode !== after.mode) fields.push('mode');
    if ([...before.tools].sort().join() !== [...after.tools].sort().join()) fields.push('tools');
    const promptChanged = before.prompt !== after.prompt;
    if (promptChanged) fields.push('prompt');
    if (fields.length) agents.changed.push({ key, name: after.name, fields, ...(promptChanged ? { promptLines: lineChanges(before.prompt, after.prompt) } : {}) });
  }
  const identical = !nodes.added.length && !nodes.removed.length && !nodes.changed.length && !edges.added.length && !edges.removed.length
    && !agents.added.length && !agents.removed.length && !agents.changed.length;
  return { nodes, edges, agents, identical };
}

/** Saved workflows with an active schedule, for the schedules view and the "next run" tile. */
export const scheduledWorkflows = <T extends { schedule: { enabled: boolean; next_run: string | null } | null }>(items: T[]) =>
  items.filter((item) => item.schedule);
