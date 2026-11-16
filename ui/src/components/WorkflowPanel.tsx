import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import { useI18n } from '../i18n';
import { clock, scheduleLabel } from '../lib/schedule';
import {
  connectWorkflow, diffVersions, editorProblem, FlowError, isConnected, MAX_AGENT_NODES, MAX_NODES, NODE_H, NODE_W, nextNodeName, removeNode, starterSpec,
  type Branch, type NodeKind, type Operator, type VersionDiff, type WorkflowNode, type WorkflowSpec, type WorkflowVersion,
} from '../lib/workflows';
import type { Agent, Schedule, WorkflowDefinition, WorkflowOverview, WorkflowRunDetail, WorkflowRunSummary } from '../types';
import Icon from './Icon';
import RunStats from './RunStats';

type Props = {
  overview: WorkflowOverview | null;
  agents: Agent[];
  formatDate: (value: string | null | undefined) => string;
  timeZone: string;
  initialRun?: string;
  onRefresh: () => Promise<void> | void;
  onOpenAgentRun: (id: string) => void;
};

type Draft = { id?: string; name: string; version: number; spec: WorkflowSpec };
const KINDS: NodeKind[] = ['agent', 'condition', 'join', 'approval'];
const ZOOMS = [0.5, 0.75, 1, 1.25];
const OPERATORS: Operator[] = ['contains', 'not_contains', 'empty', 'nonempty'];
const clamp = (value: number) => Math.max(0, Math.min(2000, Math.round(value)));
const draftOf = (item: WorkflowDefinition): Draft => ({ id: item.id, name: item.name, version: item.version, spec: structuredClone(item.spec) });

function VersionDiffView({ diff }: { diff: VersionDiff }) {
  const { t } = useI18n();
  const field = (code: string) => t('wf.diff.field.' + code);
  const Section = ({ title, items }: { title: string; items: string[] }) => (items.length ? <div className="version-diff-group"><h4>{title}</h4><ul>{items.map((item) => <li key={item}>{item}</li>)}</ul></div> : null);
  if (diff.identical) return <p className="version-diff-same"><Icon name="check" />{t('wf.diff.same')}</p>;
  return (
    <div className="version-diff">
      <Section title={t('wf.diff.nodesAdded')} items={diff.nodes.added} />
      <Section title={t('wf.diff.nodesRemoved')} items={diff.nodes.removed} />
      <Section title={t('wf.diff.nodesChanged')} items={diff.nodes.changed.map((item) => item.label + ': ' + item.fields.map(field).join(', '))} />
      <Section title={t('wf.diff.edgesAdded')} items={diff.edges.added} />
      <Section title={t('wf.diff.edgesRemoved')} items={diff.edges.removed} />
      <Section title={t('wf.diff.agentsAdded')} items={diff.agents.added} />
      <Section title={t('wf.diff.agentsRemoved')} items={diff.agents.removed} />
      {diff.agents.changed.map((item) => (
        <div key={item.key} className="version-diff-group">
          <h4>{t('wf.diff.agent')}: {item.name} · {item.fields.map(field).join(', ')}</h4>
          {item.promptLines && (
            <div className="version-diff-prompt">
              {item.promptLines.removed.map((line, index) => <p key={'r' + index} className="removed">− {line}</p>)}
              {item.promptLines.added.map((line, index) => <p key={'a' + index} className="added">+ {line}</p>)}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export default function WorkflowPanel({ overview, agents, formatDate, timeZone, initialRun, onRefresh, onOpenAgentRun }: Props) {
  const { t } = useI18n();
  const [tab, setTab] = useState<'graphs' | 'runs'>(initialRun ? 'runs' : 'graphs');
  const [draft, setDraft] = useState<Draft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [selectedNode, setSelectedNode] = useState('');
  const [linking, setLinking] = useState<{ source: string; branch: Branch } | null>(null);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState(initialRun ?? '');
  const [run, setRun] = useState<WorkflowRunDetail | null>(null);
  const startDialog = useRef<HTMLDialogElement>(null);
  const scheduleDialog = useRef<HTMLDialogElement>(null);
  const versionsDialog = useRef<HTMLDialogElement>(null);
  const [startInput, setStartInput] = useState('');
  const [consent, setConsent] = useState(false);
  const [recurring, setRecurring] = useState(false);
  const [frequency, setFrequency] = useState<Schedule['kind']>('daily');
  const [hour, setHour] = useState(9);
  const [minute, setMinute] = useState(0);
  const [weekday, setWeekday] = useState(0);
  const [versions, setVersions] = useState<WorkflowVersion[]>([]);
  const [from, setFrom] = useState(0);
  const [to, setTo] = useState(0);
  const [versionsError, setVersionsError] = useState('');

  const definitions = useMemo(() => overview?.definitions ?? [], [overview]);
  const runs = overview?.runs ?? [];
  const current = draft?.id ? definitions.find((item) => item.id === draft.id) : undefined;
  const spec = draft?.spec;
  const node = spec?.nodes.find((item) => item.id === selectedNode);
  const problem = draft && spec ? editorProblem(draft.name, spec) : null;
  const problemText = problem ? t(problem.key, problem.values) : '';
  const agentName = (id: string) => agents.find((agent) => agent.id === id)?.name ?? t('wf.unknownAgent');
  const message = (reason: unknown) => (reason instanceof FlowError ? t(reason.key, reason.values) : reason instanceof Error ? reason.message : t('wf.errGeneric'));
  const unsaved = dirty || !draft?.id;

  // The selected run, refreshed while it is active.
  useEffect(() => {
    if (!runId) { setRun(null); return; }
    let stop = false;
    const load = () => api.workflowRun(runId).then((value) => { if (!stop) setRun(value); }).catch((reason: unknown) => { if (!stop) setError(message(reason)); });
    void load();
    const timer = setInterval(() => { if (!document.hidden) void load(); }, 2500);
    return () => { stop = true; clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  function edit(change: (value: WorkflowSpec) => WorkflowSpec) {
    setDraft((value) => (value ? { ...value, spec: change(value.spec) } : value));
    setDirty(true);
  }
  function patchNode(id: string, patch: Partial<WorkflowNode>) {
    edit((value) => ({ ...value, nodes: value.nodes.map((item) => (item.id === id ? { ...item, ...patch } : item)) }));
  }
  function open(item: WorkflowDefinition) {
    if (dirty && !window.confirm(t('wf.confirmDiscard'))) return;
    setDraft(draftOf(item));
    setDirty(false);
    setSelectedNode('');
    setLinking(null);
    setError('');
    setNotice('');
    setTab('graphs');
  }
  function create() {
    if (dirty && !window.confirm(t('wf.confirmDiscard'))) return;
    const base = starterSpec();
    setDraft({ name: '', version: 0, spec: { ...base, nodes: base.nodes.map((item) => ({ ...item, label: item.kind === 'start' ? t('wf.node.start') : t('wf.node.end') })) } });
    setDirty(true);
    setSelectedNode('');
    setLinking(null);
    setError('');
    setNotice('');
    setTab('graphs');
  }
  function addNode(kind: NodeKind) {
    if (!spec) return;
    if (spec.nodes.length >= MAX_NODES) { setError(t('wf.err.nodeLimit', { max: MAX_NODES })); return; }
    if (kind === 'agent' && spec.nodes.filter((item) => item.kind === 'agent').length >= MAX_AGENT_NODES) { setError(t('wf.err.agentLimit', { max: MAX_AGENT_NODES })); return; }
    const { id, label } = nextNodeName(spec.nodes, kind, t('wf.node.' + kind));
    const y = clamp(Math.max(...spec.nodes.map((item) => item.y)) + 40);
    const added: WorkflowNode = { id, kind, label, x: clamp(80 + (spec.nodes.length % 3) * 220), y: Math.min(y, 1900), agentId: '', operator: kind === 'condition' ? 'contains' : '', value: '' };
    edit((value) => ({ ...value, nodes: [...value.nodes, added] }));
    setSelectedNode(id);
    setError('');
  }
  function connect(source: string, target: string, branch: Branch) {
    if (!spec) return false;
    try {
      edit((value) => connectWorkflow(value, source, target, branch));
      setError('');
      return true;
    } catch (reason) {
      setError(message(reason));
      return false;
    }
  }
  function onNodeClick(id: string) {
    if (linking && linking.source !== id) {
      if (connect(linking.source, id, linking.branch)) setLinking(null);
      return;
    }
    setSelectedNode(id);
  }

  async function save() {
    if (!draft || problem) return;
    setBusy(true);
    setError('');
    try {
      const saved = await api.saveWorkflow({ id: draft.id, name: draft.name.trim(), expected_version: draft.version, spec: draft.spec });
      setNotice(t('wf.saved', { version: saved.version }));
      await onRefresh();
      const fresh = (await api.workflows()).definitions.find((item) => item.id === saved.id);
      setDraft(fresh ? draftOf(fresh) : { ...draft, id: saved.id, version: saved.version });
      setDirty(false);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!draft?.id || !window.confirm(t('wf.confirmDelete', { name: draft.name }))) return;
    setBusy(true);
    try {
      await api.deleteWorkflow(draft.id);
      setDraft(null);
      setDirty(false);
      await onRefresh();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(false);
    }
  }
  async function start() {
    if (!draft?.id) return;
    setBusy(true);
    setError('');
    try {
      const created = await api.startWorkflow(draft.id, draft.version, startInput, consent);
      startDialog.current?.close();
      setStartInput('');
      setConsent(false);
      await onRefresh();
      setRunId(created.id);
      setTab('runs');
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(false);
    }
  }
  async function saveSchedule() {
    if (!draft?.id) return;
    const schedule: Schedule = frequency === 'hourly' ? { kind: 'hourly', minute } : frequency === 'daily' ? { kind: 'daily', hour, minute } : { kind: 'weekly', hour, minute, weekday };
    setBusy(true);
    setError('');
    try {
      await api.scheduleWorkflow(draft.id, { schedule, version: draft.version, input: startInput, accepted_schedule: recurring, accepted_data_flow: consent });
      scheduleDialog.current?.close();
      setNotice(t('wf.scheduleSaved'));
      await onRefresh();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(false);
    }
  }
  async function act(work: () => Promise<unknown>, done?: string) {
    setBusy(true);
    setError('');
    try {
      await work();
      if (done) setNotice(done);
      await onRefresh();
      if (runId) setRun(await api.workflowRun(runId));
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(false);
    }
  }
  const openVersions = useCallback(async () => {
    if (!draft?.id) return;
    versionsDialog.current?.showModal();
    setVersionsError('');
    try {
      const { versions: list } = await api.workflowVersions(draft.id);
      setVersions(list);
      setTo(list[0]?.version ?? 0);
      setFrom(list[1]?.version ?? list[0]?.version ?? 0);
    } catch (reason) {
      setVersionsError(message(reason));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft?.id]);
  const diff = useMemo(() => {
    const before = versions.find((item) => item.version === from);
    const after = versions.find((item) => item.version === to);
    return before && after ? diffVersions(before, after) : null;
  }, [versions, from, to]);

  // -- node dragging ------------------------------------------------------------------------
  const drag = useRef<{ id: string; startX: number; startY: number; originX: number; originY: number; moved: boolean } | null>(null);
  function onPointerDown(event: React.PointerEvent, item: WorkflowNode) {
    if (event.button !== 0) return;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    drag.current = { id: item.id, startX: event.clientX, startY: event.clientY, originX: item.x, originY: item.y, moved: false };
  }
  function onPointerMove(event: React.PointerEvent) {
    const state = drag.current;
    if (!state) return;
    const dx = event.clientX - state.startX;
    const dy = event.clientY - state.startY;
    if (!state.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    state.moved = true;
    patchNode(state.id, { x: clamp(state.originX + dx / zoom), y: clamp(state.originY + dy / zoom) });
  }
  function onPointerUp(item: WorkflowNode) {
    const state = drag.current;
    drag.current = null;
    if (!state?.moved) onNodeClick(item.id);
  }
  function onNodeKey(event: React.KeyboardEvent, item: WorkflowNode) {
    const step = event.shiftKey ? 40 : 10;
    const moves: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (moves[event.key]) {
      event.preventDefault();
      patchNode(item.id, { x: clamp(item.x + moves[event.key][0]), y: clamp(item.y + moves[event.key][1]) });
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onNodeClick(item.id);
    }
  }

  const width = spec ? Math.max(...spec.nodes.map((item) => item.x)) + NODE_W + 60 : 600;
  const height = spec ? Math.max(...spec.nodes.map((item) => item.y)) + NODE_H + 60 : 400;
  const nodeById = (id: string) => spec?.nodes.find((item) => item.id === id);
  const connected = spec ? isConnected(spec) : false;
  const kindLabel = (kind: NodeKind) => t('wf.kind.' + kind);

  // -- run view helpers ----------------------------------------------------------------------
  const runStatus = (value: string) => t('wf.status.' + value);
  const stepOf = (id: string) => run?.steps.find((step) => step.node_id === id);

  return (
    <section className="workflow-panel studio-library">
      <div className="studio-section-heading workflow-header">
        <div>
          <span className="studio-kicker">{t('wf.kicker')}</span>
          <h2>{t('nav.workflows')}</h2>
          <p>{t('wf.hint', { nodes: MAX_NODES, agents: MAX_AGENT_NODES, active: overview?.max_active_workflows ?? 3 })}</p>
        </div>
        <button className="studio-button primary" onClick={create}><Icon name="plus" />{t('wf.new')}</button>
      </div>
      {error && <div className="studio-notice error" role="alert"><Icon name="shield" />{error}<button onClick={() => setError('')} aria-label={t('common.close')}><Icon name="close" size={15} /></button></div>}
      {notice && <div className="studio-notice" role="status"><Icon name="check" />{notice}<button onClick={() => setNotice('')} aria-label={t('common.close')}><Icon name="close" size={15} /></button></div>}
      <div className="workflow-layout">
        <aside className="workflow-list" aria-label={t('wf.listLabel')}>
          <div className="workflow-list-tabs" role="tablist">
            <button role="tab" aria-selected={tab === 'graphs'} className={tab === 'graphs' ? 'selected' : ''} onClick={() => setTab('graphs')}>{t('wf.tabGraphs')} <span>{definitions.length}</span></button>
            <button role="tab" aria-selected={tab === 'runs'} className={tab === 'runs' ? 'selected' : ''} onClick={() => setTab('runs')}>{t('wf.tabRuns')} <span>{runs.length}</span></button>
          </div>
          {tab === 'graphs' ? (
            <>
              {definitions.map((item) => (
                <button key={item.id} className={'workflow-list-item' + (draft?.id === item.id ? ' selected' : '')} onClick={() => open(item)}>
                  <strong>{item.name}</strong>
                  <small>{t('wf.versionShort', { version: item.version })} · {t('wf.nodesCount', { count: item.spec.nodes.length })}</small>
                  {item.schedule && <small><Icon name="clock" size={11} /> {item.schedule.enabled ? scheduleLabel(item.schedule.schedule, t) : t('wf.schedulePaused')}</small>}
                </button>
              ))}
              {!definitions.length && <p>{t('wf.noGraphs')}</p>}
            </>
          ) : (
            <>
              {runs.map((item: WorkflowRunSummary) => (
                <button key={item.id} className={'workflow-list-item' + (runId === item.id ? ' selected' : '')} onClick={() => setRunId(item.id)}>
                  <strong>{item.name}</strong>
                  <small>{runStatus(item.status)} · {t('wf.versionShort', { version: item.version })}</small>
                  <small>{formatDate(item.created)}{item.scheduled_for ? ' · ' + t('wf.scheduledTag') : ''}</small>
                </button>
              ))}
              {!runs.length && <p>{t('wf.noRuns')}</p>}
            </>
          )}
        </aside>

        {tab === 'graphs' ? (
          <>
            <div className="workflow-main">
              {!draft || !spec ? (
                <p>{t('wf.pick')}</p>
              ) : (
                <>
                  <div className="workflow-toolbar">
                    <label>{t('wf.name')}<input aria-label={t('wf.name')} value={draft.name} maxLength={100} onChange={(event) => { setDraft({ ...draft, name: event.target.value }); setDirty(true); }} /></label>
                    {KINDS.map((kind) => <button key={kind} type="button" className="studio-button" onClick={() => addNode(kind)}><Icon name="plus" size={13} />{kindLabel(kind)}</button>)}
                    <button type="button" className="studio-icon-button" aria-label={t('wf.zoomOut')} disabled={zoom <= ZOOMS[0]} onClick={() => setZoom(ZOOMS[Math.max(0, ZOOMS.indexOf(zoom) - 1)])}><Icon name="minus" size={15} /></button>
                    <span>{Math.round(zoom * 100)}%</span>
                    <button type="button" className="studio-icon-button" aria-label={t('wf.zoomIn')} disabled={zoom >= ZOOMS[ZOOMS.length - 1]} onClick={() => setZoom(ZOOMS[Math.min(ZOOMS.length - 1, ZOOMS.indexOf(zoom) + 1)])}><Icon name="plus" size={15} /></button>
                  </div>
                  {linking && <p className="workflow-validation" role="status">{t('wf.linking', { name: nodeById(linking.source)?.label ?? '' })} <button type="button" className="studio-button" onClick={() => setLinking(null)}>{t('common.cancel')}</button></p>}
                  <div className="workflow-canvas-scroll" role="group" aria-label={t('wf.canvas')} tabIndex={0}>
                    <div style={{ width: width * zoom, height: height * zoom, position: 'relative' }}>
                      <div className="workflow-canvas" style={{ width, height, transform: `scale(${zoom})`, transformOrigin: '0 0' }}>
                        <svg className="workflow-edges" width={width} height={height} aria-hidden="true">
                          <defs><marker id="wf-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 10 5 0 10z" /></marker></defs>
                          {spec.edges.map((edge) => {
                            const a = nodeById(edge.source);
                            const b = nodeById(edge.target);
                            if (!a || !b) return null;
                            const x1 = a.x + NODE_W / 2, y1 = a.y + NODE_H, x2 = b.x + NODE_W / 2, y2 = b.y;
                            const bend = Math.max(40, Math.abs(y2 - y1) / 2);
                            return (
                              <g key={edge.source + edge.target}>
                                <path d={`M${x1} ${y1} C${x1} ${y1 + bend} ${x2} ${y2 - bend} ${x2} ${y2}`} markerEnd="url(#wf-arrow)" />
                                {edge.branch !== 'always' && <text x={(x1 + x2) / 2 + 6} y={(y1 + y2) / 2}>{edge.branch === 'true' ? t('wf.yes') : t('wf.no')}</text>}
                              </g>
                            );
                          })}
                        </svg>
                        {spec.nodes.map((item) => (
                          <div
                            key={item.id} role="button" tabIndex={0}
                            aria-label={`${item.label} (${kindLabel(item.kind)})`} aria-pressed={selectedNode === item.id}
                            className={`workflow-node ${item.kind}${selectedNode === item.id ? ' selected' : ''}${linking?.source === item.id ? ' linking' : ''}`}
                            style={{ left: item.x, top: item.y }}
                            onPointerDown={(event) => onPointerDown(event, item)} onPointerMove={onPointerMove} onPointerUp={() => onPointerUp(item)}
                            onKeyDown={(event) => onNodeKey(event, item)}
                          >
                            <small>{kindLabel(item.kind)}</small>
                            <strong>{item.label}</strong>
                            <span>{item.kind === 'agent' ? (item.agentId ? agentName(item.agentId) : t('wf.chooseAgent')) : item.kind === 'condition' ? (item.operator ? t('wf.op.' + item.operator) + (item.value ? ` “${item.value}”` : '') : t('wf.noCondition')) : ''}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                  <p className={'workflow-validation' + (problem ? ' invalid' : '')} role="status">{problem ? problemText : connected ? t('wf.valid') : t('wf.notConnected')}</p>
                  <div className="studio-actions">
                    <button type="button" className="studio-button primary" disabled={busy || !!problem || !dirty} onClick={() => void save()}><Icon name="save" size={15} />{t('wf.save')}</button>
                    <button type="button" className="studio-button" disabled={busy || unsaved || !!problem} title={unsaved ? t('wf.saveFirst') : undefined} onClick={() => { setConsent(false); startDialog.current?.showModal(); }}><Icon name="play" size={14} />{t('wf.start')}</button>
                    <button type="button" className="studio-button" disabled={busy || unsaved || !!problem} title={unsaved ? t('wf.saveFirst') : undefined} onClick={() => { setConsent(false); setRecurring(false); scheduleDialog.current?.showModal(); }}><Icon name="calendar" size={14} />{t('wf.schedule')}</button>
                    {draft.id && <button type="button" className="studio-button" onClick={() => void openVersions()}>{t('wf.compare')}</button>}
                    {draft.id && <button type="button" className="studio-button danger" disabled={busy} onClick={() => void remove()}><Icon name="trash" size={14} />{t('wf.delete')}</button>}
                  </div>
                  {current?.schedule && (
                    <div className="workflow-schedule-info" role="status">
                      <Icon name="clock" size={14} /> {scheduleLabel(current.schedule.schedule, t)} · {current.schedule.enabled ? t('wf.nextRun', { time: formatDate(current.schedule.next_run) }) : t('wf.schedulePaused')} · {t('wf.versionShort', { version: current.schedule.version })}
                      {current.schedule.last_error && <em className="studio-inline-error"> {current.schedule.last_error}</em>}
                      {current.schedule.enabled && <button type="button" className="studio-button" disabled={busy} onClick={() => void act(() => api.pauseWorkflowSchedule(current.id))}>{t('wf.pause')}</button>}
                      <button type="button" className="studio-button" disabled={busy} onClick={() => void act(() => api.removeWorkflowSchedule(current.id))}>{t('wf.removeSchedule')}</button>
                    </div>
                  )}
                </>
              )}
            </div>
            <aside className="workflow-inspector" aria-label={t('wf.inspector')}>
              {!node || !spec ? <p>{t('wf.inspectorHint')}</p> : (
                <div className="workflow-fields">
                  <h3>{kindLabel(node.kind)}</h3>
                  <label>{t('wf.stepName')}<input aria-label={t('wf.stepName')} value={node.label} maxLength={80} onChange={(event) => patchNode(node.id, { label: event.target.value })} /></label>
                  {node.kind === 'agent' && (
                    <label>{t('wf.agent')}
                      <select aria-label={t('wf.agent')} value={node.agentId} onChange={(event) => patchNode(node.id, { agentId: event.target.value })}>
                        <option value="">{t('wf.chooseAgent')}</option>
                        {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
                      </select>
                      <small>{t('wf.agentNote', { limit: overview?.max_agent_prompt ?? 2800 })}</small>
                    </label>
                  )}
                  {node.kind === 'condition' && (
                    <>
                      <label>{t('wf.operator')}
                        <select aria-label={t('wf.operator')} value={node.operator} onChange={(event) => patchNode(node.id, { operator: event.target.value as Operator })}>
                          {OPERATORS.map((op) => <option key={op} value={op}>{t('wf.op.' + op)}</option>)}
                        </select>
                      </label>
                      {(node.operator === 'contains' || node.operator === 'not_contains') && (
                        <label>{t('wf.value')}<input aria-label={t('wf.value')} value={node.value} maxLength={200} onChange={(event) => patchNode(node.id, { value: event.target.value })} /></label>
                      )}
                      <small>{t('wf.conditionNote')}</small>
                    </>
                  )}
                  {node.kind === 'approval' && <small>{t('wf.approvalNote')}</small>}
                  {node.kind === 'join' && <small>{t('wf.joinNote')}</small>}
                  {node.kind === 'end' && <small>{t('wf.endNote')}</small>}
                  {node.kind !== 'end' && (
                    <LinkStarter node={node} onStart={(branch) => setLinking({ source: node.id, branch })} />
                  )}
                  <IncomingLink node={node} nodes={spec.nodes} onConnect={connect} />
                  <h3>{t('wf.edges')}</h3>
                  {spec.edges.filter((edge) => edge.source === node.id || edge.target === node.id).map((edge) => (
                    <div className="workflow-edge-item" key={edge.source + edge.target}>
                      <span>{nodeById(edge.source)?.label} → {nodeById(edge.target)?.label}{edge.branch !== 'always' ? ` (${edge.branch === 'true' ? t('wf.yes') : t('wf.no')})` : ''}</span>
                      <button type="button" className="studio-icon-button" aria-label={t('wf.removeEdge')} onClick={() => edit((value) => ({ ...value, edges: value.edges.filter((item) => item !== edge) }))}><Icon name="close" size={13} /></button>
                    </div>
                  ))}
                  {node.kind !== 'start' && node.kind !== 'end' && (
                    <button type="button" className="studio-button danger" onClick={() => { edit((value) => removeNode(value, node.id)); setSelectedNode(''); }}><Icon name="trash" size={14} />{t('wf.removeNode')}</button>
                  )}
                </div>
              )}
            </aside>
          </>
        ) : (
          <div className="workflow-main workflow-run-view">
            {!run ? <p>{runId ? t('common.loading') : t('wf.pickRun')}</p> : (
              <>
                <div className="workflow-toolbar">
                  <strong>{run.name}</strong>
                  <span className={'studio-badge ' + (run.status === 'completed' ? 'success' : run.status === 'running' ? 'running' : 'failed')}>{runStatus(run.status)}</span>
                  {run.status === 'running' && <button className="studio-button danger" disabled={busy} onClick={() => window.confirm(t('wf.confirmCancel')) && void act(() => api.cancelWorkflow(run.id))}>{t('wf.cancel')}</button>}
                  {run.can_continue && <button className="studio-button" disabled={busy} onClick={() => void act(() => api.continueWorkflow(run.id))}>{t('wf.continue')}</button>}
                  {run.status !== 'running' && <button className="studio-button" disabled={busy} onClick={() => window.confirm(t('wf.confirmRerun')) && void act(async () => { const created = await api.rerunWorkflow(run.id, true); setRunId(created.id); })}><Icon name="refresh" size={14} />{t('wf.rerun')}</button>}
                </div>
                {run.error && <div className="studio-inline-error workflow-stopped" role="alert">{run.error}</div>}
                <dl className="workflow-run-facts">
                  <div><dt>{t('wf.fact.version')}</dt><dd>{t('wf.versionShort', { version: run.version })}</dd></div>
                  <div><dt>{t('wf.fact.started')}</dt><dd>{formatDate(run.created)}</dd></div>
                  <div><dt>{t('wf.fact.updated')}</dt><dd>{formatDate(run.updated)}</dd></div>
                  <div><dt>{t('wf.fact.outcome')}</dt><dd>{t('outcome.' + run.outcome_status)}</dd></div>
                  <div><dt>{t('wf.fact.trigger')}</dt><dd>{run.scheduled_for ? t('wf.scheduledAt', { time: formatDate(run.scheduled_for) }) : t('run.manualStart')}</dd></div>
                </dl>
                <details className="workflow-pinned-prompt"><summary>{t('wf.input')}</summary><pre className="workflow-output">{run.input || t('wf.noInput')}</pre></details>
                <RunStats key={'wf-stats-' + run.id} metrics={run.metrics} />
                <h3>{t('wf.steps')}</h3>
                <ol className="workflow-steps">
                  {run.spec.nodes.map((item) => {
                    const step = stepOf(item.id);
                    const state = step?.status ?? 'pending';
                    return (
                      <li key={item.id} className={'workflow-step ' + state}>
                        <strong>{item.label}</strong> <small>{kindLabel(item.kind)}</small>
                        <span className={'workflow-step-state ' + state}>{t('wf.step.' + state)}</span>
                        {item.kind === 'agent' && <small>{run.agents[item.id]?.name ?? ''}</small>}
                        {step?.status === 'approval' && (
                          <span className="studio-actions">
                            <button className="studio-button primary" disabled={busy} onClick={() => void act(() => api.approveWorkflow(run.id, item.id, true))}><Icon name="check" size={14} />{t('wf.approve')}</button>
                            <button className="studio-button danger" disabled={busy} onClick={() => void act(() => api.approveWorkflow(run.id, item.id, false))}>{t('wf.reject')}</button>
                          </span>
                        )}
                        {step?.child_id && <button className="studio-button" onClick={() => onOpenAgentRun(step.child_id as string)}>{t('wf.openAgentRun')}</button>}
                        {step?.metrics && <small>{t('wf.stepCalls', { model: step.metrics.model_calls, tools: step.metrics.tool_calls })}</small>}
                        {step?.output && <details><summary>{t('wf.output')}</summary><pre className="workflow-output">{step.output}</pre></details>}
                      </li>
                    );
                  })}
                </ol>
              </>
            )}
          </div>
        )}
      </div>

      <dialog ref={startDialog} className="studio-dialog workflow-run-dialog" aria-labelledby="wf-start-title">
        <div className="studio-dialog-heading"><div><span className="studio-kicker">{t('wf.kicker')}</span><h2 id="wf-start-title">{t('wf.startTitle', { name: draft?.name ?? '' })}</h2></div><button type="button" className="studio-icon-button" onClick={() => startDialog.current?.close()} aria-label={t('common.close')}><Icon name="close" /></button></div>
        <div className="studio-dialog-body workflow-fields">
          <label>{t('wf.input')}<textarea value={startInput} maxLength={4000} rows={5} onChange={(event) => setStartInput(event.target.value)} placeholder={t('wf.inputPh')} /></label>
          <label className="studio-consent"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /><span><strong>{t('wf.consentTitle')}</strong> {t('wf.consentBody')}</span></label>
          <p className="studio-caption">{t('wf.startNote', { minutes: Math.round((overview?.timeout_seconds ?? 3600) / 60) })}</p>
        </div>
        <div className="studio-dialog-footer"><button type="button" className="studio-button primary" disabled={busy || !consent} onClick={() => void start()}><Icon name="play" size={14} />{t('wf.start')}</button></div>
      </dialog>

      <dialog ref={scheduleDialog} className="studio-dialog workflow-run-dialog" aria-labelledby="wf-schedule-title">
        <div className="studio-dialog-heading"><div><span className="studio-kicker">{t('wf.kicker')}</span><h2 id="wf-schedule-title">{t('wf.scheduleTitle', { name: draft?.name ?? '' })}</h2></div><button type="button" className="studio-icon-button" onClick={() => scheduleDialog.current?.close()} aria-label={t('common.close')}><Icon name="close" /></button></div>
        <div className="studio-dialog-body workflow-fields">
          <label>{t('composer.repeat')}
            <select value={frequency} onChange={(event) => setFrequency(event.target.value as Schedule['kind'])}>
              <option value="hourly">{t('composer.hourly')}</option><option value="daily">{t('composer.daily')}</option><option value="weekly">{t('composer.weekly')}</option>
            </select>
          </label>
          {frequency === 'hourly' ? (
            <label>{t('composer.minute', { zone: timeZone })}<input type="number" min={0} max={59} value={minute} onChange={(event) => { const value = Number(event.target.value); if (Number.isInteger(value) && value >= 0 && value <= 59) setMinute(value); }} /></label>
          ) : (
            <label>{t('composer.time', { zone: timeZone })}<input type="time" value={clock({ hour, minute })} onChange={(event) => { const [h, m] = event.target.value.split(':').map(Number); if (Number.isInteger(h) && Number.isInteger(m)) { setHour(h); setMinute(m); } }} /></label>
          )}
          {frequency === 'weekly' && <label>{t('composer.weekday')}<select value={weekday} onChange={(event) => setWeekday(Number(event.target.value))}>{Array.from({ length: 7 }, (_, index) => <option key={index} value={index}>{t('weekday.' + index)}</option>)}</select></label>}
          <label>{t('wf.input')}<textarea value={startInput} maxLength={4000} rows={3} onChange={(event) => setStartInput(event.target.value)} placeholder={t('wf.inputPh')} /></label>
          <label className="studio-consent"><input type="checkbox" checked={recurring} onChange={(event) => setRecurring(event.target.checked)} /><span><strong>{t('composer.consentTitle')}</strong> {t('composer.consentBody')}</span></label>
          <label className="studio-consent"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /><span><strong>{t('wf.consentTitle')}</strong> {t('wf.consentBody')}</span></label>
          <p className="studio-caption">{t('wf.scheduleNote', { zone: timeZone })}</p>
        </div>
        <div className="studio-dialog-footer"><button type="button" className="studio-button primary" disabled={busy || !consent || !recurring} onClick={() => void saveSchedule()}><Icon name="calendar" size={14} />{t('wf.scheduleSave')}</button></div>
      </dialog>

      <dialog ref={versionsDialog} className="studio-dialog workflow-run-dialog" aria-labelledby="wf-versions-title">
        <div className="studio-dialog-heading"><div><span className="studio-kicker">{t('wf.diff.kicker')}</span><h2 id="wf-versions-title">{t('wf.compare')}</h2></div><button type="button" className="studio-icon-button" onClick={() => versionsDialog.current?.close()} aria-label={t('common.close')}><Icon name="close" /></button></div>
        <div className="studio-dialog-body">
          {versionsError && <p className="studio-notice error" role="alert">{versionsError}</p>}
          {!versionsError && versions.length < 2 && <p>{t('wf.diff.onlyOne')}</p>}
          {versions.length >= 2 && (
            <>
              <div className="version-diff-pickers">
                <label>{t('wf.diff.older')}<select value={from} onChange={(event) => setFrom(Number(event.target.value))}>{versions.map((item) => <option key={item.version} value={item.version}>v{item.version} · {formatDate(item.created)}</option>)}</select></label>
                <label>{t('wf.diff.newer')}<select value={to} onChange={(event) => setTo(Number(event.target.value))}>{versions.map((item) => <option key={item.version} value={item.version}>v{item.version} · {formatDate(item.created)}</option>)}</select></label>
              </div>
              {from === to ? <p>{t('wf.diff.pickTwo')}</p> : diff && <VersionDiffView diff={diff} />}
              <p className="studio-caption">{t('wf.diff.note')}</p>
            </>
          )}
        </div>
      </dialog>
    </section>
  );
}

/** Start a link from this node: a condition first needs the yes/no branch. */
function LinkStarter({ node, onStart }: { node: WorkflowNode; onStart: (branch: Branch) => void }) {
  const { t } = useI18n();
  const [branch, setBranch] = useState<'true' | 'false'>('true');
  return (
    <div className="workflow-fields workflow-incoming">
      {node.kind === 'condition' && (
        <label>{t('wf.branch')}<select aria-label={t('wf.branch')} value={branch} onChange={(event) => setBranch(event.target.value as 'true' | 'false')}><option value="true">{t('wf.yes')}</option><option value="false">{t('wf.no')}</option></select></label>
      )}
      <button type="button" className="studio-button" onClick={() => onStart(node.kind === 'condition' ? branch : 'always')}>{t('wf.linkFrom')}</button>
    </div>
  );
}

/** Add an incoming link without dragging, from a list of earlier steps (keyboard and touch friendly). */
function IncomingLink({ node, nodes, onConnect }: { node: WorkflowNode; nodes: WorkflowNode[]; onConnect: (source: string, target: string, branch: Branch) => boolean }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState('');
  const [branch, setBranch] = useState<'true' | 'false'>('true');
  const sourceNode = nodes.find((item) => item.id === source);
  if (node.kind === 'start') return null;
  return (
    <div className="workflow-fields workflow-incoming">
      {node.kind === 'end' && <p>{t('wf.endNote')}</p>}
      <button type="button" className="studio-button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>{t('wf.linkTo')}</button>
      {open && (
        <>
          <label>{t('wf.previous')}
            <select aria-label={t('wf.previous')} value={source} onChange={(event) => { setSource(event.target.value); setBranch('true'); }}>
              <option value="">{t('wf.pickSource')}</option>
              {nodes.filter((item) => item.id !== node.id && item.kind !== 'end').map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
          </label>
          {sourceNode?.kind === 'condition' && (
            <label>{t('wf.branch')}<select aria-label={t('wf.branch')} value={branch} onChange={(event) => setBranch(event.target.value as 'true' | 'false')}><option value="true">{t('wf.yes')}</option><option value="false">{t('wf.no')}</option></select></label>
          )}
          <button type="button" className="studio-button" disabled={!sourceNode} onClick={() => { if (onConnect(source, node.id, sourceNode?.kind === 'condition' ? branch : 'always')) { setOpen(false); setSource(''); } }}>{t('wf.connect')}</button>
        </>
      )}
    </div>
  );
}
