import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { useI18n } from '../i18n';
import { formatCount } from '../lib/metrics';
import { outcomeLabel } from '../lib/outcome';
import { summaryFigures, type DailySummary } from '../lib/summary';
import type { Outcome } from '../types';
import Icon from './Icon';

type Props = { formatDate: (value: string | null | undefined) => string; onOpenWorkflowRun: (id: string) => void };

/** "What happened today": counts and causes only, never content. */
export default function DailySummaryView({ formatDate, onOpenWorkflowRun }: Props) {
  const { t } = useI18n();
  const [data, setData] = useState<DailySummary | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.summary());
      setError('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('summary.errGeneric'));
    } finally {
      setLoading(false);
    }
  }, [t]);
  useEffect(() => {
    void load();
    const timer = setInterval(() => { if (!document.hidden) void load(); }, 30000);
    return () => clearInterval(timer);
  }, [load]);

  if (!data) {
    return (
      <section className={'studio-connecting' + (error ? ' is-error' : '')} role={error ? 'alert' : 'status'} aria-busy={loading}>
        <h2>{error ? t('summary.unavailable') : t('summary.loading')}</h2>
        {error && <><p>{error}</p><button type="button" className="studio-button primary" onClick={() => void load()}><Icon name="refresh" />{t('app.retry')}</button></>}
      </section>
    );
  }
  const figures = summaryFigures(data);
  const percent = figures.cachePercent === null ? '' : ` · ${t('summary.cache', { percent: figures.cachePercent })}`;
  return (
    <section className="daily-summary" aria-label={t('summary.title')}>
      {error && <p className="studio-notice error" role="alert">{error}</p>}
      <header>
        <div><h2>{t('summary.title')}</h2><p>{t('summary.hint', { since: formatDate(data.since) })}</p></div>
        <button type="button" className="studio-button" onClick={() => void load()} disabled={loading}><Icon name="refresh" />{t('summary.refresh')}</button>
      </header>
      {data.limit.paused_until && <p className="studio-notice" role="status"><Icon name="clock" />{t('notice.limitPaused', { time: formatDate(data.limit.paused_until) })}</p>}
      <div className="daily-summary-grid">
        <article>
          <small>{t('summary.workflows')}</small><strong>{data.workflows.total}</strong>
          <p>{t('summary.ok', { count: figures.flowOk })} · <b className={figures.flowStopped ? 'bad' : ''}>{t('summary.stopped', { count: figures.flowStopped })}</b>{figures.flowRunning ? ` · ${t('summary.running', { count: figures.flowRunning })}` : ''}</p>
        </article>
        <article>
          <small>{t('summary.agents')}</small><strong>{data.agents.total}</strong>
          <p>{t('summary.ok', { count: figures.agentsOk })} · <b className={figures.agentsStopped ? 'bad' : ''}>{t('summary.stopped', { count: figures.agentsStopped })}</b>{figures.agentsRunning ? ` · ${t('summary.running', { count: figures.agentsRunning })}` : ''}</p>
        </article>
        <article>
          <small>{t('summary.calls')}</small><strong>{formatCount(data.tokens.calls)}</strong>
          <p>{t('summary.callErrors', { count: data.tokens.error_calls })}{data.limit.rate_limited_calls ? ` · ${t('summary.rateLimited', { count: data.limit.rate_limited_calls })}` : ''}</p>
        </article>
        <article>
          <small>{t('summary.tokens')}</small><strong>{formatCount(data.tokens.input + data.tokens.output)}</strong>
          <p>{t('summary.tokensDetail', { input: formatCount(data.tokens.input), output: formatCount(data.tokens.output) })}{percent}</p>
        </article>
      </div>
      <h3>{t('summary.problems')}</h3>
      {data.workflows.problems.length ? (
        <ul className="daily-summary-problems">
          {data.workflows.problems.map((item) => (
            <li key={item.id}>
              <div>
                <strong>{item.name || t('summary.unnamed')}</strong>
                <span>{outcomeLabel(item.outcome as Outcome, t)} · {formatDate(item.updated)}</span>
                <p>{item.reason || t('summary.noReason')}</p>
              </div>
              <button type="button" className="studio-button" onClick={() => onOpenWorkflowRun(item.id)}>{t('summary.open')}</button>
            </li>
          ))}
        </ul>
      ) : <p className="daily-summary-clear"><Icon name="check" />{t('summary.none')}</p>}
    </section>
  );
}
