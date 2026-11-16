import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from './api';
import AgentLibrary from './components/AgentLibrary';
import Composer, { type ComposerHandle, type ComposerResult } from './components/Composer';
import Icon from './components/Icon';
import KnowledgePanel from './components/KnowledgePanel';
import WorkflowPanel from './components/WorkflowPanel';
import RunArtifacts from './components/RunArtifacts';
import RunGraph from './components/RunGraph';
import RunStats from './components/RunStats';
import RuntimeDialog from './components/RuntimeDialog';
import { LANGUAGES, useI18n } from './i18n';
import { formatDate, runTitle } from './lib/format';
import { outcomeBlocked, outcomeLabel, outcomeVisual } from './lib/outcome';
import { nextScheduledRun } from './lib/schedule';
import { toolLabelById } from './lib/tools';
import type { AgentTemplate } from './lib/templates';
import { useTheme } from './theme';
import type { Agent, Run, ServiceStatus, ToolInfo, UploadedFile, WorkflowOverview } from './types';

// The live thread pulls in assistant-ui and the LangGraph SDK (most of the bundle): load it on demand.
const LiveThread = lazy(() => import('./components/LiveThread'));

type View = 'workspace' | 'library' | 'schedules' | 'knowledge' | 'workflows';
type Pane = 'graph' | 'conversation' | 'events';
type Snapshot = { runs: Run[]; agents: Agent[]; status: ServiceStatus };

export default function App({ onLanguage }: { onLanguage: (language: string | undefined) => void }) {
  const { t, lang, setLang } = useI18n();
  const { theme, toggle: toggleTheme } = useTheme();
  const [data, setData] = useState<Snapshot | null>(null);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [detail, setDetail] = useState<Run | null>(null);
  const [statusError, setStatusError] = useState('');
  const [needsLogin, setNeedsLogin] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [view, setView] = useState<View>('workspace');
  const [pane, setPane] = useState<Pane>('graph');
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [liveVersion, setLiveVersion] = useState(0);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('all');
  const [railTab, setRailTab] = useState<'runs' | 'agents'>('runs');
  const [account, setAccount] = useState('');
  const [workflows, setWorkflows] = useState<WorkflowOverview | null>(null);
  const [workflowRun, setWorkflowRun] = useState('');
  const composer = useRef<ComposerHandle>(null);
  const runtime = useRef<HTMLDialogElement>(null);

  const refresh = useCallback(async () => {
    try {
      const [runs, agents, status] = await Promise.all([api.runs(), api.agents(), api.status()]);
      setData({ runs, agents, status });
      setStatusError('');
      setNeedsLogin(false);
      api.workflows().then(setWorkflows).catch(() => undefined);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) setNeedsLogin(true);
      else setStatusError(reason instanceof Error ? reason.message : t('app.connectionError'));
    }
  }, [t]);
  const refreshFiles = useCallback(async () => {
    try {
      setFiles(await api.files());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('composer.errGeneric'));
    }
  }, [t]);

  useEffect(() => {
    void refresh();
    void refreshFiles();
    api.tools().then(setTools).catch(() => undefined);
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 5000);
    return () => clearInterval(timer);
  }, [refresh, refreshFiles]);

  const status = data?.status ?? null;
  useEffect(() => {
    onLanguage(status?.language);
    if (status?.auth_enabled) api.me().then((me) => setAccount(me.email)).catch(() => undefined);
  }, [status?.language, status?.auth_enabled, onLanguage]);

  const zone = status?.schedule_timezone ?? 'UTC';
  const runs = useMemo(() => data?.runs ?? [], [data]);
  const agents = useMemo(() => data?.agents ?? [], [data]);
  const max = status?.max_concurrent_runs ?? 3;
  const active = runs.filter((item) => item.status === 'running').length;
  const listed = selected ? runs.find((item) => item.id === selected) : runs[0];
  const run: Run | undefined = listed ? (detail?.id === listed.id ? { ...listed, ...detail, metrics: listed.metrics ?? detail.metrics } : listed) : undefined;
  const next = nextScheduledRun([
    ...agents,
    ...(workflows?.definitions ?? []).filter((item) => item.schedule).map((item) => ({ name: item.name, enabled: !!item.schedule?.enabled, next_run: item.schedule?.next_run ?? null })),
  ]);
  const schedulerReady = !!status?.scheduler_heartbeat && Date.now() - Date.parse(status.scheduler_heartbeat) < 90_000;
  const attention = runs.filter((item) => item.status === 'failed' || outcomeBlocked(item.outcome_status)).length;
  const lower = (value: string) => value.toLocaleLowerCase(lang);
  const visibleRuns = runs.filter(
    (item) =>
      (!search || lower(item.prompt).includes(lower(search))) &&
      (filter === 'all' || (filter === 'active' && item.status === 'running') || (filter === 'completed' && item.status === 'completed') || (filter === 'failed' && ['failed', 'cancelled'].includes(item.status))),
  );
  const date = (value: string | number | Date | null | undefined) => formatDate(value, zone, lang);

  // The selected run's detail (result, files). Active runs refresh every 3 s; finished ones once per change.
  const detailId = run?.id;
  const detailKey = run?.updated;
  const detailActive = run?.status === 'running';
  useEffect(() => {
    if (!detailId) return;
    let stop = false;
    const load = () => api.run(detailId).then((value) => { if (!stop) setDetail(value); }).catch(() => undefined);
    void load();
    if (!detailActive) return () => { stop = true; };
    const timer = setInterval(() => { if (!document.hidden) void load(); }, 3000);
    return () => { stop = true; clearInterval(timer); };
  }, [detailId, detailKey, detailActive]);

  function openRun(id: string, target: Pane = 'graph') {
    setSelected(id);
    setView('workspace');
    setPane(target);
    setRailTab('runs');
  }
  async function perform<T>(work: () => Promise<T>, message?: string): Promise<T | null> {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await work();
      if (message) setNotice(message);
      await refresh();
      return result;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('composer.errGeneric'));
      return null;
    } finally {
      setBusy(false);
    }
  }
  function onComposerDone(result: ComposerResult) {
    if (result.kind === 'run') openRun(result.run.id);
    else {
      setView('library');
      const touched = result.agent.updated_workflows ?? 0;
      setNotice(touched ? t('wf.agentEditNotice', { count: touched }) : result.editing ? t('notice.agentUpdated') : t('notice.agentSaved'));
    }
    void refresh();
    void refreshFiles();
  }
  const openComposer = (kind: 'run' | 'agent', options?: { from?: Run; edit?: Agent; template?: AgentTemplate }) => {
    setError('');
    setNotice('');
    composer.current?.open(kind, options);
  };
  async function rerun(item: Run) {
    const created = await perform(() => api.rerun(item.id));
    if (created) openRun(created.id);
  }
  async function runAgent(id: string) {
    const created = await perform(() => api.runAgent(id));
    if (created) openRun(created.id);
  }
  function toggleSchedule(agent: Agent) {
    if (!agent.enabled && !window.confirm(t('confirm.enableSchedule'))) return;
    void perform(() => api.enableAgent(agent.id, !agent.enabled));
  }
  function deleteAgent(agent: Agent) {
    if (window.confirm(t('confirm.deleteAgent', { name: agent.name }))) void perform(() => api.deleteAgent(agent.id));
  }
  function cancelRun(item: Run) {
    if (window.confirm(t('confirm.cancelRun'))) void perform(() => api.cancel(item.id));
  }
  function download() {
    if (!run?.result) return;
    const url = URL.createObjectURL(new Blob([run.result], { type: 'text/markdown;charset=utf-8' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `agent-${run.id}.md`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function logout() {
    await api.logout().catch(() => undefined);
    setAccount('');
    setData(null);
    setNeedsLogin(true);
  }

  const libraryProps = {
    agents, tools, files, timeZone: zone, busy, canStart: !!status && active < max, runIds: runs.map((item) => item.id),
    onRun: (id: string) => void runAgent(id), onEdit: (agent: Agent) => openComposer('agent', { edit: agent }), onToggle: toggleSchedule,
    onDelete: deleteAgent, onLastRun: (id: string) => openRun(id), onNew: () => openComposer('agent'),
    onTemplate: (template: AgentTemplate) => openComposer('agent', { template }),
  };
  const runStatusLabel = (value: string) => t('run.status.' + value);
  const toolName = (id: string) => toolLabelById(id, tools, t);

  return (
    <div className="agents-page">
      <a className="studio-skip" href="#studio-main">{t('app.skip')}</a>
      <div className="agent-studio">
        <header className="studio-header">
          <div className="studio-heading">
            <span className="studio-brand-icon"><Icon name="graph" size={25} /></span>
            <div>
              <div className="studio-breadcrumb">{t('app.breadcrumb')} <Icon name="chevron" size={10} /> AGENT STUDIO</div>
              <h1>Deep Agents <span>Studio</span></h1>
            </div>
          </div>
          <div className="studio-header-actions">
            <span className="studio-backend-state"><i className={'studio-dot ' + (status ? 'is-ready' : '')} />{status ? t('app.backendReady') : t('app.connecting')}</span>
            {account && <span className="studio-account" title={account}>{account}</span>}
            <button className="studio-toggle" onClick={() => setLang(LANGUAGES.find((code) => code !== lang) ?? 'en')} aria-label={t('app.switchLanguage')} title={t('app.switchLanguage')}>{(LANGUAGES.find((code) => code !== lang) ?? 'en').toUpperCase()}</button>
            <button className="studio-toggle" onClick={toggleTheme} aria-label={theme === 'dark' ? t('app.lightTheme') : t('app.darkTheme')} title={theme === 'dark' ? t('app.lightTheme') : t('app.darkTheme')}><Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} /></button>
            <button className="studio-icon-button" onClick={() => runtime.current?.showModal()} aria-label={t('runtime.open')}><Icon name="shield" /></button>
            {status?.auth_enabled && <button className="studio-icon-button" onClick={() => void logout()} aria-label={t('app.logout')} title={t('app.logout')}><Icon name="logout" /></button>}
            <button className="studio-button primary" onClick={() => openComposer('run')}><Icon name="plus" />{t('app.newTask')}</button>
          </div>
        </header>

        <main id="studio-main">
          {needsLogin ? (
            <section className="studio-connecting studio-login" role="status">
              <span className="studio-connecting-orbit"><Icon name="shield" size={26} /></span>
              <h2>{t('login.title')}</h2>
              <p>{t('login.body')}</p>
              <a className="studio-button primary" href="/auth/login"><Icon name="arrow" size={15} />{t('login.button')}</a>
            </section>
          ) : !data ? (
            <section className={'studio-connecting' + (statusError ? ' is-error' : '')} role={statusError ? 'alert' : 'status'} aria-live="polite" aria-busy={!statusError}>
              <span className="studio-connecting-orbit" aria-hidden="true"><Icon name={statusError ? 'shield' : 'activity'} size={26} /></span>
              <h2>{statusError ? t('app.unreachableTitle') : t('app.connectingTitle')}</h2>
              <p>{statusError ? statusError + ' ' + t('app.unreachableBody') : t('app.connectingBody')}</p>
              {statusError ? (
                <button type="button" className="studio-button primary" onClick={() => { setStatusError(''); void refresh(); }}><Icon name="refresh" />{t('app.retry')}</button>
              ) : (
                <div className="studio-connecting-bars" aria-hidden="true"><i /><i /><i /></div>
              )}
            </section>
          ) : (
            <>
              <section className="studio-overview" aria-label={t('overview.label')}>
                <div>
                  <span className="studio-metric-icon mint"><Icon name="activity" size={18} /></span>
                  <div><small>{t('overview.active')}</small><strong>{active}<em> / {max}</em></strong></div>
                  <div className="studio-capacity" aria-label={t('overview.slots', { active, max })}>{Array.from({ length: max }, (_, index) => <i key={index} className={index < active ? 'filled' : ''} />)}</div>
                </div>
                <div><span className="studio-metric-icon violet"><Icon name="layers" size={18} /></span><div><small>{t('overview.agents')}</small><strong>{agents.length}</strong></div></div>
                <div><span className="studio-metric-icon amber"><Icon name="shield" size={18} /></span><div><small>{t('overview.attention')}</small><strong>{attention}</strong></div></div>
                <div>
                  <span className="studio-metric-icon cyan"><Icon name="clock" size={18} /></span>
                  <div>
                    <small>{t('overview.next')}</small>
                    <strong className="studio-next-time" title={next ? next.name : undefined}>{next ? date(next.when) : t('overview.noSchedule')}</strong>
                    {next && <small className="studio-next-name">{next.name}</small>}
                  </div>
                </div>
              </section>

              <nav className="studio-navigation" aria-label={t('nav.label')}>
                {([{ id: 'workspace', icon: 'graph', label: t('nav.workspace') }, { id: 'library', icon: 'layers', label: t('nav.library') }, { id: 'schedules', icon: 'calendar', label: t('nav.schedules') }, { id: 'workflows', icon: 'activity', label: t('nav.workflows') }, { id: 'knowledge', icon: 'file', label: t('nav.knowledge') }] as const).map((item) => (
                  <button key={item.id} className={view === item.id ? 'selected' : ''} aria-current={view === item.id ? 'page' : undefined} onClick={() => setView(item.id)}>
                    <Icon name={item.icon} size={16} />{item.label}{item.id === 'library' && <span>{agents.length}</span>}
                  </button>
                ))}
                <span className="studio-scheduler-state"><i className={'studio-dot ' + (schedulerReady ? 'is-ready' : '')} />{!status?.scheduler_enabled ? t('runtime.schedulerOff') : schedulerReady ? t('app.schedulerActive') : t('app.noHeartbeat')}</span>
              </nav>

              {(error || statusError) && (
                <div className="studio-notice error" role="alert"><Icon name="shield" />{error || statusError}
                  <button onClick={() => { setError(''); setStatusError(''); }} aria-label={t('common.close')}><Icon name="close" size={15} /></button></div>
              )}
              {notice && <div className="studio-notice" role="status"><Icon name="check" />{notice}<button onClick={() => setNotice('')} aria-label={t('common.close')}><Icon name="close" size={15} /></button></div>}
              {!!status?.limit_paused_until && Date.parse(status.limit_paused_until) > Date.now() && (
                <div className="studio-notice" role="status"><Icon name="clock" />{t('notice.limitPaused', { time: date(status.limit_paused_until) })}</div>
              )}

              {view === 'workspace' ? (
                <div className="studio-workspace">
                  <aside className="studio-rail" aria-label={t('rail.label')}>
                    <div className="studio-rail-tabs">
                      <button className={railTab === 'runs' ? 'selected' : ''} onClick={() => setRailTab('runs')}>{t('rail.runs')} <span>{runs.length}</span></button>
                      <button className={railTab === 'agents' ? 'selected' : ''} onClick={() => setRailTab('agents')}>{t('rail.agents')} <span>{agents.length}</span></button>
                    </div>
                    <label className="studio-search compact"><Icon name="search" size={15} /><input aria-label={t('rail.search')} placeholder={t('rail.searchPh')} value={search} onChange={(event) => setSearch(event.target.value)} /></label>
                    {railTab === 'runs' && (
                      <select className="studio-run-filter" aria-label={t('rail.filter')} value={filter} onChange={(event) => setFilter(event.target.value)}>
                        <option value="all">{t('rail.filterAll')}</option><option value="active">{t('rail.filterActive')}</option>
                        <option value="completed">{t('rail.filterCompleted')}</option><option value="failed">{t('rail.filterFailed')}</option>
                      </select>
                    )}
                    <div className="studio-run-list agent-runs">
                      {railTab === 'runs'
                        ? visibleRuns.map((item) => (
                            <button key={item.id} className={'studio-run-item' + (run?.id === item.id ? ' selected' : '')} aria-pressed={run?.id === item.id} onClick={() => setSelected(item.id)}>
                              <span className={'studio-run-dot ' + outcomeVisual(item.status, item.outcome_status)} />
                              <span className="studio-run-copy">
                                <strong>{runTitle(item.prompt, t)}</strong>
                                <small>{runStatusLabel(item.status)}</small>
                                <span>
                                  <time dateTime={item.created}>{date(item.created)}</time>
                                  <em>{item.mode === 'test' ? t('mode.testTag') : t('mode.liveTag')}</em>
                                  {item.workflow ? <em className="studio-run-agent-tag">{t('run.workflowTag')}</em> : item.agent_id && <em className="studio-run-agent-tag">{t('run.agentTag')}</em>}
                                </span>
                              </span>
                            </button>
                          ))
                        : agents.filter((agent) => lower(agent.name + ' ' + agent.config.prompt).includes(lower(search))).map((agent) => (
                            <button key={agent.id} className="studio-rail-agent" onClick={() => setView('library')}>
                              <span className="studio-avatar small"><Icon name="spark" size={15} /></span>
                              <span><strong>{agent.name}</strong><small>{agent.enabled ? t('rail.scheduled') : t('rail.manual')}</small></span>
                              <Icon name="chevron" size={12} />
                            </button>
                          ))}
                      {railTab === 'runs' && !visibleRuns.length && <div className="studio-rail-empty"><Icon name="activity" size={23} /><p>{runs.length ? t('rail.noMatch') : t('rail.noRuns')}</p></div>}
                      {railTab === 'agents' && !agents.length && <div className="studio-rail-empty"><Icon name="layers" size={23} /><p>{t('rail.noAgents')}</p></div>}
                    </div>
                    <div className="studio-rail-footer"><Icon name="shield" size={13} />{t('rail.footer', { minutes: Math.round((status?.run_timeout_seconds ?? 3600) / 60) })}</div>
                  </aside>

                  <section className="studio-stage">
                    {run ? (
                      <>
                        <header className="studio-run-heading">
                          <div>
                            <div className="studio-run-heading-meta">
                              <span className={'studio-badge ' + outcomeVisual(run.status, run.outcome_status)}><i className={'studio-run-dot ' + outcomeVisual(run.status, run.outcome_status)} />{runStatusLabel(run.status)}</span>
                              <span>{run.mode === 'test' ? t('mode.test') : run.model}</span>
                            </div>
                            {run.workflow && (
                              <p className="studio-run-origin">
                                <span>{t('run.workflowPart')} <strong>{run.workflow.name}</strong> · {run.workflow.node_label}</span>
                                <button type="button" className="studio-button" onClick={() => { setWorkflowRun(run.workflow?.id ?? ''); setView('workflows'); }}>{t('run.openInWorkflow')}</button>
                              </p>
                            )}
                            {run.agent_id && !run.workflow && (
                              <p className="studio-run-origin">
                                <span>{t('run.partOfAgent')} <strong>{agents.find((agent) => agent.id === run.agent_id)?.name ?? run.agent_id.slice(0, 8)}</strong></span>
                                {agents.some((agent) => agent.id === run.agent_id) && <button type="button" className="studio-button" onClick={() => setView('library')}>{t('run.openInLibrary')}</button>}
                              </p>
                            )}
                            <h2 title={run.prompt}>{runTitle(run.prompt, t)}</h2>
                            <p><code>{run.id.slice(0, 8)}</code><span>·</span>{date(run.created)}<span>·</span>{t('run.steps', { count: run.steps })}</p>
                          </div>
                          <div className="studio-run-heading-actions">
                            <button className="studio-icon-button" disabled={busy || active >= max} onClick={() => void rerun(run)} aria-label={t('run.rerun')} title={t('run.rerunTitle')}><Icon name="refresh" /></button>
                            <button className="studio-icon-button" disabled={busy} onClick={() => openComposer('agent', { from: run })} aria-label={t('run.saveAsAgent')} title={t('run.saveAsAgentTitle')}><Icon name="save" /></button>
                            {run.result && <button className="studio-icon-button" onClick={download} aria-label={t('run.download')} title={t('run.downloadTitle')}><Icon name="download" /></button>}
                            {run.status === 'running' && <button className="studio-icon-button danger" disabled={busy} onClick={() => cancelRun(run)} aria-label={t('run.cancel')}><Icon name="close" /></button>}
                          </div>
                        </header>
                        <div
                          className="studio-stage-tabs" role="tablist" aria-label={t('run.viewLabel')}
                          onKeyDown={(event) => {
                            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || (event.target as HTMLElement).getAttribute('role') !== 'tab') return;
                            event.preventDefault();
                            const order: Pane[] = ['graph', 'conversation', 'events'];
                            const index = order.indexOf(pane);
                            const target = event.key === 'Home' ? order[0] : event.key === 'End' ? order[2] : order[(index + (event.key === 'ArrowRight' ? 1 : 2)) % 3];
                            setPane(target);
                            requestAnimationFrame(() => document.getElementById('studio-tab-' + target)?.focus());
                          }}
                        >
                          {([{ id: 'graph', label: t('run.tabGraph'), icon: 'graph' }, { id: 'conversation', label: t('run.tabMessages'), icon: 'message' }, { id: 'events', label: t('run.tabEvents'), icon: 'activity' }] as const).map((item) => (
                            <button key={item.id} role="tab" tabIndex={pane === item.id ? 0 : -1} id={'studio-tab-' + item.id} aria-selected={pane === item.id} aria-controls={item.id === 'conversation' ? 'studio-pane-conversation' : 'studio-pane-canvas'} className={pane === item.id ? 'selected' : ''} onClick={() => setPane(item.id)}>
                              <Icon name={item.icon} size={15} />{item.label}
                            </button>
                          ))}
                          <button className="studio-reconnect" aria-label={t('run.reconnect')} onClick={() => setLiveVersion((version) => version + 1)}><Icon name="refresh" size={14} /></button>
                        </div>
                        {run.error && <div className="studio-inline-error" role="alert">{run.error}</div>}
                        {run.status === 'completed' && (
                          <p className={outcomeBlocked(run.outcome_status) ? 'studio-inline-error' : 'studio-caption'} role="status">{t('run.outcomeLine', { outcome: outcomeLabel(run.outcome_status, t) })}</p>
                        )}
                        <RunStats key={'stats-' + run.id} metrics={run.metrics} runId={run.id} />
                        <RunArtifacts key={run.id} run={run} />
                        <div hidden={pane === 'conversation'} role="tabpanel" id="studio-pane-canvas" aria-labelledby={'studio-tab-' + (pane === 'events' ? 'events' : 'graph')}>
                          <RunGraph key={run.id + '-' + liveVersion} run={run} view={pane === 'events' ? 'events' : 'graph'} />
                        </div>
                        <div hidden={pane !== 'conversation'} className="studio-conversation" id="studio-pane-conversation" role="tabpanel" aria-labelledby="studio-tab-conversation">
                          <div className="studio-context-tags">
                            <span><Icon name="shield" size={13} />{run.tools.length ? run.tools.map(toolName).join(' · ') : t('run.noExternalTools')}</span>
                            <span><Icon name="file" size={13} />{t('run.attachments', { count: run.files?.length ?? 0 })}</span>
                          </div>
                          <Suspense fallback={<p>{t('common.loading')}</p>}><LiveThread key={run.id + '-' + liveVersion} run={run} onSettled={refresh} /></Suspense>
                          {run.result && <details className="studio-report-source"><summary>{t('run.reportSource')}</summary><pre className="agent-report">{run.result}</pre></details>}
                          <details className="studio-report-source"><summary>{t('run.originalPrompt')}</summary><pre className="agent-report">{run.prompt}</pre></details>
                          <details className="studio-run-provenance">
                            <summary>{t('run.provenance')}</summary>
                            <dl className="studio-facts">
                              <div><dt>{t('run.id')}</dt><dd>{run.id}</dd></div>
                              <div><dt>{t('run.rerunOf')}</dt><dd>{run.rerun_of || '—'}</dd></div>
                              <div><dt>{t('run.scheduledFor')}</dt><dd>{run.scheduled_for ? date(run.scheduled_for) : t('run.manualStart')}</dd></div>
                              <div><dt>{t('run.tools')}</dt><dd>{run.tool_calls ?? 0}</dd></div>
                            </dl>
                          </details>
                        </div>
                      </>
                    ) : selected && busy ? (
                      <div className="studio-workspace-empty" aria-busy="true">
                        <span className="studio-empty-orbit"><Icon name="activity" size={26} /></span>
                        <h2>{t('empty.loadingRun')}</h2><p>{t('empty.loadingRunBody')}</p>
                      </div>
                    ) : (
                      <div className="studio-workspace-empty">
                        <div className="studio-empty-graphic" aria-hidden="true"><span><Icon name="file" size={24} /></span><i /><span className="highlight"><Icon name="spark" size={30} /></span><i /><span><Icon name="check" size={24} /></span></div>
                        <span className="studio-kicker">{t('empty.kicker')}</span>
                        <h2>{t('empty.title')}</h2>
                        <p>{t('empty.body')}</p>
                        <div className="studio-actions">
                          <button className="studio-button primary" onClick={() => openComposer('run')}><Icon name="plus" />{t('empty.first')}</button>
                          <button className="studio-button" onClick={() => setView('library')}>{t('nav.library')}<Icon name="arrow" size={14} /></button>
                        </div>
                        <small>{t('empty.note', { max })}</small>
                      </div>
                    )}
                  </section>
                </div>
              ) : view === 'workflows' ? (
                <WorkflowPanel
                  key={'wf-' + workflowRun} overview={workflows} agents={agents} timeZone={zone} initialRun={workflowRun || undefined}
                  formatDate={date} onRefresh={() => api.workflows().then(setWorkflows).catch(() => undefined)} onOpenAgentRun={(id) => openRun(id)}
                />
              ) : view === 'knowledge' ? (
                <KnowledgePanel formatDate={date} />
              ) : (
                <AgentLibrary key={view} {...libraryProps} scheduled={view === 'schedules'} />
              )}
              <footer className="studio-footer">
                <button className="studio-footer-settings" onClick={() => runtime.current?.showModal()}><Icon name="shield" size={12} />{t('app.connectionSecurity')}</button>
                <span>Deep Agents + LangGraph <i /> assistant-ui runtime</span>
              </footer>
            </>
          )}
        </main>
      </div>
      <Composer ref={composer} tools={tools} files={files} status={status} active={active} onDone={onComposerDone} onFilesChanged={() => void refreshFiles()} />
      <RuntimeDialog ref={runtime} status={status} schedulerReady={schedulerReady} account={account} />
    </div>
  );
}
