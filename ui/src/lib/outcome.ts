import type { Outcome } from '../types';
import type { T } from './schedule';

export const outcomeLabel = (value: Outcome | undefined, t: T) => t('outcome.' + (value || 'unknown'));
export const outcomeBlocked = (value: Outcome | undefined) => value === 'blocked' || value === 'error';
/** CSS state of a run: a technically completed run whose business outcome is blocked looks failed, unproven looks amber. */
export function outcomeVisual(status: string, outcome: Outcome | undefined): string {
  if (status !== 'completed') return status;
  if (outcomeBlocked(outcome)) return 'failed';
  return !outcome || outcome === 'unknown' ? 'interrupted' : 'completed';
}
