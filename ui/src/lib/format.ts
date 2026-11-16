import type { T } from './schedule';

/** Short date-time in the schedule time zone, e.g. "Nov 16, 08:30". */
export function formatDate(value: string | number | Date | null | undefined, timeZone: string, language: string): string {
  if (value === null || value === undefined) return '–';
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) return '–';
  try {
    return date.toLocaleString(language, { timeZone, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch {
    return date.toLocaleString(language, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
}

export const runTitle = (prompt: string, t: T) => prompt.split('\n').find((line) => line.trim())?.slice(0, 90) || t('run.untitled');
export const kilobytes = (size: number) => Math.max(1, Math.ceil(size / 1024));
