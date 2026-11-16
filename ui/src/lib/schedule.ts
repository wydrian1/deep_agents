import type { Agent, Schedule } from '../types';

/** Translation function: a dictionary key plus optional ``{name}`` values. */
export type T = (key: string, values?: Record<string, string | number>) => string;

const two = (value: number) => String(value).padStart(2, '0');

/** HH:MM wall time; for hourly schedules only the minute is meaningful, see scheduleTime. */
export const clock = (schedule: Pick<Schedule, 'hour' | 'minute'>) => `${two(schedule.hour ?? 0)}:${two(schedule.minute)}`;
/** Time shown on a schedule tile: HH:MM, or :MM for "every hour at minute MM". */
export const scheduleTime = (schedule: Schedule) => (schedule.kind === 'hourly' ? ':' + two(schedule.minute) : clock(schedule));

export function scheduleLabel(schedule: Schedule | null, t: T): string {
  if (!schedule) return t('schedule.manual');
  if (schedule.kind === 'hourly') return t('schedule.hourlyLabel', { minute: two(schedule.minute) });
  const time = clock(schedule);
  return schedule.kind === 'daily'
    ? t('schedule.dailyLabel', { time })
    : t('schedule.weeklyLabel', { day: t('weekday.' + (schedule.weekday ?? 0)), time });
}

/** Short summary for the composer; hourly ignores the hour field. */
export function scheduleSummary(schedule: Schedule | null, t: T): string {
  if (!schedule) return t('schedule.manualShort');
  if (schedule.kind === 'hourly') return t('schedule.hourlyShort', { minute: two(schedule.minute) });
  const time = clock(schedule);
  return schedule.kind === 'daily' ? t('schedule.dailyShort', { time }) : t('weekday.' + (schedule.weekday ?? 0)) + ' · ' + time;
}

/** Whether an enabled schedule produces a run on the given weekday (0 = Monday). */
export const runsOnWeekday = (schedule: Schedule | null, weekday: number) =>
  !!schedule && (schedule.kind === 'hourly' || schedule.kind === 'daily' || (schedule.kind === 'weekly' && (schedule.weekday ?? 0) === weekday));

/** The next automatic start across saved agents. A paused agent has no next_run and is never counted. */
export function nextScheduledRun(agents: Pick<Agent, 'name' | 'enabled' | 'next_run'>[]): { when: Date; name: string } | null {
  const candidates = agents
    .filter((agent) => agent.enabled && agent.next_run)
    .map((agent) => ({ when: new Date(agent.next_run as string), name: agent.name }))
    .filter((item) => Number.isFinite(item.when.getTime()));
  return candidates.sort((a, b) => a.when.getTime() - b.when.getTime())[0] ?? null;
}
