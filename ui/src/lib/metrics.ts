import type { RunMetrics } from '../types';
import type { T } from './schedule';

export type TimeShare = { key: 'startup' | 'model' | 'tool' | 'other'; label: string; ms: number; percent: number };

const group = (digits: string) => digits.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
export const formatCount = (value: number | null | undefined) =>
  typeof value === 'number' && Number.isFinite(value) ? group(String(Math.round(value))) : '–';

export function formatDuration(ms: number | null | undefined, t: T): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return '–';
  if (ms < 1000) return Math.round(ms) + ' ms';
  if (ms < 60000) return t('format.seconds', { value: (Math.round(ms / 100) / 10).toFixed(1) });
  const total = Math.round(ms / 1000);
  return t('format.minutes', { minutes: Math.floor(total / 60), seconds: String(total % 60).padStart(2, '0') });
}

/** Share of input tokens served from the provider cache; null when input is unknown or zero. */
export function cachePercent(metrics: Pick<RunMetrics, 'input_tokens' | 'cache_read_tokens'>): number | null {
  const { input_tokens: input, cache_read_tokens: cached } = metrics;
  if (input === null || cached === null || input <= 0) return null;
  return Math.round(Math.min(100, Math.max(0, (cached / input) * 100)));
}

/** Time breakdown of one run; empty when the split is not measured. */
export function timeShares(metrics: RunMetrics, t: T): TimeShare[] {
  if (!metrics.measured || metrics.model_ms === null || metrics.startup_ms === null || metrics.other_ms === null) return [];
  const parts: [TimeShare['key'], number][] = [
    ['startup', metrics.startup_ms], ['model', metrics.model_ms], ['tool', metrics.tool_ms], ['other', metrics.other_ms],
  ];
  const total = parts.reduce((sum, [, ms]) => sum + ms, 0);
  if (total <= 0) return [];
  return parts.map(([key, ms]) => ({ key, label: t('stats.share.' + key), ms, percent: Math.round((ms / total) * 100) }));
}

export function metricsSummaryText(metrics: RunMetrics, t: T): string {
  const tokens = metrics.input_tokens === null ? t('stats.tokensUnknown') : t('stats.tokens', { count: formatCount((metrics.input_tokens || 0) + (metrics.output_tokens || 0)) });
  return formatDuration(metrics.wall_ms, t) + ' · ' + t('stats.callsShort', { count: metrics.model_calls }) + ' · ' + tokens;
}
