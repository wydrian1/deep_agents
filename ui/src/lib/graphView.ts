import type { StudioEdge, StudioEvent, StudioNode } from '../types';
import type { T } from './schedule';

/** Keep the newest 160 step events and 40 message events, in order (the previous Studio's bound). */
export function mergeStudioEvents(current: StudioEvent[], incoming: StudioEvent[]): StudioEvent[] {
  const unique = [...new Map([...current, ...incoming].map((event) => [event.id, event])).values()].sort((a, b) => a.id - b.id);
  return [...unique.filter((e) => e.kind !== 'message').slice(-160), ...unique.filter((e) => e.kind === 'message').slice(-40)].sort((a, b) => a.id - b.id);
}

const KNOWN = new Set(['__start__', '__end__', 'model', 'tools']);
export function nodeLabel(id: string, t: T): string {
  if (KNOWN.has(id)) return t('node.' + id);
  const middleware: Record<string, string> = {
    'PatchToolCallsMiddleware.before_agent': 'node.patchTools',
    'TodoListMiddleware.after_model': 'node.todos',
    '_ParentNotifierMiddleware.after_agent': 'node.close',
  };
  if (middleware[id]) return t(middleware[id]);
  return id.replace(/Middleware\./g, ' · ').replace(/([a-z])([A-Z])/g, '$1 $2').slice(0, 90);
}

export type LaidOutNode = StudioNode & { label: string; x: number; y: number };
/** Layered layout: breadth-first levels from ``__start__``, nodes of a level centred in a row. */
export function graphLayout(nodes: (StudioNode & { label: string })[], edges: StudioEdge[]) {
  const levels = new Map<string, number>();
  const start = nodes.find((node) => node.id === '__start__')?.id || nodes[0]?.id;
  if (start) {
    levels.set(start, 0);
    const queue = [start];
    while (queue.length) {
      const id = queue.shift() as string;
      for (const edge of edges) {
        if (edge.source !== id || levels.has(edge.target)) continue;
        levels.set(edge.target, (levels.get(id) as number) + 1);
        queue.push(edge.target);
      }
    }
  }
  let last = Math.max(0, ...levels.values());
  for (const node of nodes) if (!levels.has(node.id)) levels.set(node.id, ++last);
  const groups = new Map<number, (StudioNode & { label: string })[]>();
  for (const node of nodes) {
    const level = levels.get(node.id) as number;
    groups.set(level, [...(groups.get(level) || []), node]);
  }
  const columns = Math.max(1, ...[...groups.values()].map((group) => group.length));
  const width = Math.max(520, columns * 228 + 100);
  const height = Math.max(370, (Math.max(0, ...levels.values()) + 1) * 100 + 50);
  const positioned: LaidOutNode[] = nodes.map((node) => {
    const group = groups.get(levels.get(node.id) as number) as (StudioNode & { label: string })[];
    const index = group.indexOf(node);
    return { ...node, x: width / 2 + (index - (group.length - 1) / 2) * 228 - 90, y: (levels.get(node.id) as number) * 100 + 25 };
  });
  return { nodes: positioned, width, height };
}
