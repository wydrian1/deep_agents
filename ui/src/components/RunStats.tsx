import { useEffect, useState } from 'react';
import { api } from '../api';
import { useI18n } from '../i18n';
import { cachePercent, formatCount, formatDuration, metricsSummaryText, timeShares } from '../lib/metrics';
import type { RunMetrics, RunMetricsDetail } from '../types';

function CallDetails({ runId, calls }: { runId: string; calls: number }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<RunMetricsDetail | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    api
      .metrics(runId, controller.signal)
      .then((body) => {
        setDetail(body);
        setError('');
      })
      .catch((reason: unknown) => {
        if ((reason as { name?: string })?.name !== 'AbortError') setError(reason instanceof Error && reason.message ? reason.message : t('stats.loadFailed'));
      });
    return () => controller.abort();
  }, [open, runId, calls, t]);
  const start = (ms: number | null) => (ms === null ? '–' : '+' + formatDuration(ms, t));
  return (
    <details className="run-stats-detail" onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}>
      <summary>{t('stats.details')}</summary>
      {open && !detail && !error && <p className="run-stats-note" role="status">{t('common.loading')}</p>}
      {error && <p className="studio-inline-error" role="alert">{error}</p>}
      {detail && (
        <>
          <div className="run-stats-scroll" tabIndex={0} role="region" aria-label={t('stats.callsTable')}>
            <table>
              <caption>{t('stats.callsTable')}</caption>
              <thead>
                <tr>
                  <th scope="col">#</th><th scope="col">{t('stats.col.start')}</th><th scope="col">{t('stats.col.time')}</th>
                  <th scope="col">{t('stats.col.input')}</th><th scope="col">{t('stats.col.output')}</th><th scope="col">{t('stats.col.cache')}</th>
                  <th scope="col">{t('stats.col.reasoning')}</th><th scope="col">{t('stats.col.scope')}</th><th scope="col">{t('stats.col.result')}</th>
                </tr>
              </thead>
              <tbody>
                {detail.calls.map((call) => (
                  <tr key={call.seq}>
                    <td>{call.seq}</td><td>{start(call.offset_ms)}</td><td>{formatDuration(call.duration_ms, t)}</td>
                    <td>{formatCount(call.input_tokens)}</td><td>{formatCount(call.output_tokens)}</td><td>{formatCount(call.cache_read_tokens)}</td>
                    <td>{formatCount(call.reasoning_tokens)}</td><td>{call.scope === 'subagent' ? t('stats.subtask') : t('stats.main')}</td>
                    <td className={call.status === 'ok' ? '' : 'run-stats-error'} title={call.error || undefined}>
                      {call.status === 'ok' ? t('stats.ok') : t('stats.failed') + (call.http_status ? ` (${call.http_status})` : '')}
                    </td>
                  </tr>
                ))}
                {!detail.calls.length && <tr><td colSpan={9}>{t('stats.noCalls')}</td></tr>}
              </tbody>
            </table>
          </div>
          {!!detail.tools.length && (
            <div className="run-stats-scroll" tabIndex={0} role="region" aria-label={t('stats.toolsTable')}>
              <table>
                <caption>{t('stats.toolsTable')}</caption>
                <thead>
                  <tr><th scope="col">{t('stats.col.tool')}</th><th scope="col">{t('stats.col.operation')}</th><th scope="col">{t('stats.col.start')}</th><th scope="col">{t('stats.col.time')}</th><th scope="col">{t('stats.col.result')}</th></tr>
                </thead>
                <tbody>
                  {detail.tools.map((tool, index) => (
                    <tr key={index}>
                      <td>{tool.tool}</td><td>{tool.operation}</td><td>{start(tool.offset_ms)}</td><td>{formatDuration(tool.duration_ms, t)}</td>
                      <td className={tool.outcome === 'ok' ? '' : 'run-stats-error'}>{tool.outcome === 'ok' ? t('stats.ok') : tool.outcome === 'started' ? t('stats.interrupted') : t('stats.failed')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {detail.truncated && <p className="run-stats-note">{t('stats.truncated', { count: detail.calls.length })}</p>}
          <p className="run-stats-note">{t('stats.contentFree')}</p>
        </>
      )}
    </details>
  );
}

export default function RunStats({ metrics, runId }: { metrics?: RunMetrics | null; runId?: string | null }) {
  const { t } = useI18n();
  if (!metrics) return null;
  const shares = timeShares(metrics, t);
  const cache = cachePercent(metrics);
  const partialUsage = metrics.model_calls > 0 && metrics.usage_calls < metrics.model_calls;
  return (
    <details className="run-stats">
      <summary><strong>{t('stats.title')}</strong><span>{metricsSummaryText(metrics, t)}</span></summary>
      {metrics.model_calls === 0 && <p className="run-stats-note">{t('stats.noCallsYet')}</p>}
      <dl className="run-stats-grid">
        <div><dt>{t('stats.wall')}</dt><dd>{formatDuration(metrics.wall_ms, t)}</dd></div>
        <div><dt>{t('stats.model')}</dt><dd>{formatDuration(metrics.model_ms, t)}</dd></div>
        <div><dt>{t('stats.tool')}</dt><dd>{formatDuration(metrics.tool_ms, t)}</dd></div>
        <div><dt>{t('stats.startup')}</dt><dd>{formatDuration(metrics.startup_ms, t)}</dd></div>
        <div><dt>{t('stats.modelCalls')}</dt><dd>{metrics.model_calls}{metrics.subagent_calls > 0 && <small> · {t('stats.ofSubtasks', { count: metrics.subagent_calls })}</small>}</dd></div>
        <div><dt>{t('stats.toolCalls')}</dt><dd>{metrics.tool_calls}</dd></div>
        <div><dt>{t('stats.errorCalls')}</dt><dd className={metrics.error_calls ? 'run-stats-error' : ''}>{metrics.error_calls}</dd></div>
        <div><dt>{t('stats.inputTokens')}</dt><dd>{formatCount(metrics.input_tokens)}</dd></div>
        <div><dt>{t('stats.outputTokens')}</dt><dd>{formatCount(metrics.output_tokens)}</dd></div>
        <div><dt>{t('stats.cached')}</dt><dd>{formatCount(metrics.cache_read_tokens)}{cache !== null && <small> · {cache}%</small>}</dd></div>
        <div><dt>{t('stats.reasoningTokens')}</dt><dd>{formatCount(metrics.reasoning_tokens)}</dd></div>
      </dl>
      {partialUsage && <p className="run-stats-note">{t('stats.partialUsage', { used: metrics.usage_calls, total: metrics.model_calls })}</p>}
      {metrics.input_tokens !== null && metrics.cache_read_tokens === 0 && <p className="run-stats-note">{t('stats.noCacheHits')}</p>}
      {shares.length > 0 && (
        <div className="run-stats-split">
          <div className="run-stats-bar" role="img" aria-label={t('stats.splitLabel') + ': ' + shares.map((s) => `${s.label.split(' (')[0]} ${s.percent}%`).join(', ')}>
            {shares.filter((s) => s.ms > 0).map((s) => <span key={s.key} className={'seg ' + s.key} style={{ flex: s.ms + ' 0 0' }} />)}
          </div>
          <ul className="run-stats-legend">
            {shares.map((s) => <li key={s.key}><i className={'swatch ' + s.key} aria-hidden="true" />{s.label}: {formatDuration(s.ms, t)} ({s.percent}%)</li>)}
          </ul>
        </div>
      )}
      {runId && metrics.model_calls > 0 && <CallDetails runId={runId} calls={metrics.model_calls} />}
    </details>
  );
}
