import { describe, expect, it } from 'vitest';
import { translate } from '../i18n';
import type { RunMetrics, StudioEvent, ToolInfo } from '../types';
import { formatDate, runTitle } from './format';
import { graphLayout, mergeStudioEvents, nodeLabel } from './graphView';
import { cachePercent, formatCount, formatDuration, metricsSummaryText, timeShares } from './metrics';
import { outcomeBlocked, outcomeVisual } from './outcome';
import { clock, nextScheduledRun, runsOnWeekday, scheduleLabel, scheduleSummary, scheduleTime } from './schedule';
import { toggleTool } from './tools';

import { summaryFigures, type DailySummary } from './summary';

const hu = (key: string, values?: Record<string, string | number>) => translate('hu', key, values);
const en = (key: string, values?: Record<string, string | number>) => translate('en', key, values);

describe('schedule labels and rules', () => {
  it('formats times and labels in both languages', () => {
    expect(clock({ hour: 8, minute: 5 })).toBe('08:05');
    expect(scheduleTime({ kind: 'hourly', minute: 7 })).toBe(':07');
    expect(scheduleTime({ kind: 'daily', hour: 9, minute: 30 })).toBe('09:30');
    expect(scheduleLabel(null, en)).toBe('Manual start');
    expect(scheduleLabel({ kind: 'hourly', minute: 5 }, en)).toBe('Hourly · at minute 05 of every hour');
    expect(scheduleLabel({ kind: 'weekly', weekday: 0, hour: 9, minute: 0 }, hu)).toBe('Hétfő hetente · 09:00');
    expect(scheduleLabel({ kind: 'daily', hour: 8, minute: 30 }, hu)).toBe('Naponta · 08:30');
    expect(scheduleSummary({ kind: 'weekly', weekday: 6, hour: 7, minute: 15 }, en)).toBe('Sunday · 07:15');
    expect(scheduleSummary(null, hu)).toBe('Kézi');
  });
  it('decides which weekdays a schedule runs on (Monday = 0)', () => {
    expect(runsOnWeekday(null, 0)).toBe(false);
    expect(runsOnWeekday({ kind: 'daily', hour: 1, minute: 0 }, 5)).toBe(true);
    expect(runsOnWeekday({ kind: 'hourly', minute: 0 }, 3)).toBe(true);
    expect(runsOnWeekday({ kind: 'weekly', weekday: 2, hour: 1, minute: 0 }, 2)).toBe(true);
    expect(runsOnWeekday({ kind: 'weekly', weekday: 2, hour: 1, minute: 0 }, 3)).toBe(false);
  });
  it('picks the earliest enabled next run and ignores paused agents', () => {
    const agents = [
      { name: 'later', enabled: true, next_run: '2026-11-17T08:00:00+00:00' },
      { name: 'paused', enabled: false, next_run: '2026-11-16T01:00:00+00:00' },
      { name: 'soon', enabled: true, next_run: '2026-11-16T09:00:00+00:00' },
      { name: 'none', enabled: true, next_run: null },
      { name: 'bad', enabled: true, next_run: 'not a date' },
    ];
    expect(nextScheduledRun(agents)?.name).toBe('soon');
    expect(nextScheduledRun([])).toBeNull();
  });
});

describe('graph helpers', () => {
  const event = (id: number, kind: StudioEvent['kind']): StudioEvent => ({ id, node: 'model', namespace: '', kind, step: null, summary: '', detail: '' });
  it('merges events by id and keeps the newest 160 steps and 40 messages', () => {
    const steps = Array.from({ length: 200 }, (_, i) => event(i + 1, 'update'));
    const messages = Array.from({ length: 60 }, (_, i) => event(1000 + i, 'message'));
    const merged = mergeStudioEvents(steps.slice(0, 150), [...steps.slice(100), ...messages, event(5, 'update')]);
    expect(merged.filter((e) => e.kind === 'update')).toHaveLength(160);
    expect(merged.filter((e) => e.kind === 'message')).toHaveLength(40);
    expect(merged.map((e) => e.id)).toEqual([...merged.map((e) => e.id)].sort((a, b) => a - b));
    expect(new Set(merged.map((e) => e.id)).size).toBe(merged.length);
  });
  it('labels known nodes and tidies unknown middleware names', () => {
    expect(nodeLabel('model', en)).toBe('Model');
    expect(nodeLabel('__start__', hu)).toBe('Feladat');
    expect(nodeLabel('TodoListMiddleware.after_model', en)).toBe('Todo update');
    expect(nodeLabel('SomeCustomMiddleware.before_agent', en)).toBe('Some Custom · before_agent');
  });
  it('lays nodes out in levels from the start node, wider levels centred', () => {
    const nodes = ['__start__', 'a', 'b', 'c', '__end__', 'orphan'].map((id) => ({ id, label: id }));
    const edges = [
      { source: '__start__', target: 'a' }, { source: '__start__', target: 'b' }, { source: 'a', target: 'c' },
      { source: 'b', target: 'c' }, { source: 'c', target: '__end__' },
    ];
    const layout = graphLayout(nodes, edges);
    const at = (id: string) => layout.nodes.find((n) => n.id === id)!;
    expect(at('__start__').y).toBeLessThan(at('a').y);
    expect(at('a').y).toBe(at('b').y);
    expect(at('a').x).toBeLessThan(at('b').x);
    expect(at('c').y).toBeLessThan(at('__end__').y);
    expect(at('orphan').y).toBeGreaterThan(at('__end__').y);
    expect(layout.width).toBeGreaterThanOrEqual(520);
    expect(graphLayout([], []).nodes).toEqual([]);
  });
});

describe('metrics formatting', () => {
  it('formats counts and durations (locale-aware), unknown as a dash', () => {
    expect(formatCount(1234567)).toBe('1 234 567');
    expect(formatCount(null)).toBe('–');
    expect(formatDuration(850, en)).toBe('850 ms');
    expect(formatDuration(2350, en)).toBe('2.4 s');
    expect(formatDuration(2350, hu)).toBe('2.4 mp');
    expect(formatDuration(125_000, en)).toBe('2 min 05 s');
    expect(formatDuration(null, en)).toBe('–');
    expect(formatDuration(-1, en)).toBe('–');
  });
  const metrics: RunMetrics = {
    wall_ms: 10_000, startup_ms: 1000, model_ms: 6000, tool_ms: 2000, other_ms: 1000, model_calls: 3, subagent_calls: 0, error_calls: 0,
    tool_calls: 2, input_tokens: 1000, output_tokens: 200, cache_read_tokens: 250, reasoning_tokens: null, usage_calls: 3, measured: true,
  };
  it('computes the cache share, the time split and the summary line', () => {
    expect(cachePercent(metrics)).toBe(25);
    expect(cachePercent({ input_tokens: null, cache_read_tokens: 1 })).toBeNull();
    expect(cachePercent({ input_tokens: 0, cache_read_tokens: 0 })).toBeNull();
    const shares = timeShares(metrics, en);
    expect(shares.map((s) => s.percent)).toEqual([10, 60, 20, 10]);
    expect(timeShares({ ...metrics, measured: false }, en)).toEqual([]);
    expect(timeShares({ ...metrics, model_ms: null }, en)).toEqual([]);
    expect(metricsSummaryText(metrics, en)).toBe('10.0 s · 3 model calls · 1 200 tokens');
    expect(metricsSummaryText({ ...metrics, input_tokens: null }, en)).toContain('tokens: –');
  });
});

describe('outcome visuals', () => {
  it('shows a blocked completed run as failed and an unproven one as amber', () => {
    expect(outcomeVisual('running', undefined)).toBe('running');
    expect(outcomeVisual('completed', 'blocked')).toBe('failed');
    expect(outcomeVisual('completed', 'error')).toBe('failed');
    expect(outcomeVisual('completed', 'unknown')).toBe('interrupted');
    expect(outcomeVisual('completed', 'success')).toBe('completed');
    expect(outcomeVisual('completed', 'noop')).toBe('completed');
    expect(outcomeBlocked('blocked')).toBe(true);
    expect(outcomeBlocked('success')).toBe(false);
  });
});

describe('tool selection', () => {
  const catalog: ToolInfo[] = [
    { name: 'files', label: 'Files', access: 'read', description: '', requires: [] },
    { name: 'document', label: 'Document', access: 'read', description: '', requires: ['files'] },
    { name: 'file_search', label: 'Search', access: 'read', description: '', requires: ['files'] },
    { name: 'webfetch', label: 'Web', access: 'read', description: '', requires: [] },
  ];
  it('adds required tools when selecting and removes dependants when deselecting', () => {
    const selected = toggleTool([], 'document', catalog);
    expect(new Set(selected)).toEqual(new Set(['document', 'files']));
    const more = toggleTool(selected, 'file_search', catalog);
    expect(new Set(more)).toEqual(new Set(['document', 'files', 'file_search']));
    expect(toggleTool(more, 'files', catalog)).toEqual([]);
    expect(toggleTool(['webfetch', 'files'], 'webfetch', catalog)).toEqual(['files']);
  });
});

describe('format helpers', () => {
  it('formats dates in the given zone and survives bad input', () => {
    const budapest = formatDate('2026-11-16T07:30:00+00:00', 'Europe/Budapest', 'en');
    const utc = formatDate('2026-11-16T07:30:00+00:00', 'UTC', 'en');
    expect(budapest).toContain('8:30');
    expect(utc).toContain('7:30');
    expect(formatDate(null, 'UTC', 'en')).toBe('–');
    expect(formatDate('garbage', 'UTC', 'en')).toBe('–');
    expect(formatDate('2026-11-16T07:30:00+00:00', 'Not/AZone', 'en')).not.toBe('–');
  });
  it('titles a run by its first non-empty line, at most 90 characters', () => {
    expect(runTitle('\n  \nHello world\nsecond', en)).toBe('Hello world');
    expect(runTitle('x'.repeat(200), en)).toHaveLength(90);
    expect(runTitle('   ', en)).toBe('Untitled task');
  });
});

describe('daily summary figures', () => {
  const summary: DailySummary = {
    since: '2026-11-16T00:00:00+00:00', now: '2026-11-16T10:00:00+00:00',
    agents: { total: 5, by_status: { completed: 3, failed: 1, running: 1 }, by_outcome: { success: 2, blocked: 1 } },
    workflows: { total: 6, by_status: { completed: 3, failed: 1, running: 1, cancelled: 1 }, by_outcome: {}, problem_count: 1, problems: [] },
    tokens: { calls: 10, input: 1000, output: 200, cache: 250, error_calls: 1, cache_rate: 0.25 },
    limit: { rate_limited_calls: 0, paused_until: null },
  };
  it('splits fine, stuck and running without counting cancelled workflows as fine', () => {
    expect(summaryFigures(summary)).toEqual({ flowOk: 3, flowStopped: 1, flowRunning: 1, agentsOk: 2, agentsStopped: 2, agentsRunning: 1, cachePercent: 25 });
    expect(summaryFigures({ ...summary, tokens: { ...summary.tokens, cache_rate: null } }).cachePercent).toBeNull();
  });
});
