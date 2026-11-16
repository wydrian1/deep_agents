import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import { useI18n } from '../i18n';
import { graphLayout, mergeStudioEvents, nodeLabel } from '../lib/graphView';
import type { Run, StudioEdge, StudioEvent, StudioGraph, StudioNode } from '../types';
import Icon from './Icon';

/** Default text of a step event whose message had no text (the server sends an empty summary). */
const defaultSummary = (event: StudioEvent, t: (key: string) => string) =>
  event.summary || (event.kind === 'interrupt' ? t('graph.waitingApproval') : t('graph.stateUpdate'));

export default function RunGraph({ run, view }: { run: Run; view: 'graph' | 'events' }) {
  const { t } = useI18n();
  const [graph, setGraph] = useState<StudioGraph | null>(null);
  const [events, setEvents] = useState<StudioEvent[]>([]);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('model');
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [latest, setLatest] = useState<Record<string, StudioEvent>>({});
  const [observed, setObserved] = useState<{ nodes: StudioNode[]; edges: StudioEdge[] }>({ nodes: [], edges: [] });
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [dataTab, setDataTab] = useState(false);
  const [focusedEvent, setFocusedEvent] = useState<StudioEvent | null>(null);
  const [showMessages, setShowMessages] = useState(false);
  const cursor = useRef(0);
  const previous = useRef('');
  const known = useRef(new Set<string>());
  const links = useRef(new Set<string>());
  const drag = useRef<{ x: number; y: number; pan: { x: number; y: number } } | null>(null);
  const marker = useId().replace(/:/g, '');
  const active = run.status === 'running';

  useEffect(() => {
    let cancelled = false;
    let busy = false;
    const controller = new AbortController();
    async function load() {
      if (busy || cancelled || document.hidden) return;
      busy = true;
      try {
        let more = true;
        while (more && !cancelled) {
          const result = await api.graph(run.id, cursor.current, controller.signal);
          if (cancelled) return;
          cursor.current = result.cursor;
          more = result.hasMore;
          setGraph(result);
          setError('');
          if (result.events.length) {
            setEvents((current) => mergeStudioEvents(current, result.events));
            const increments: Record<string, number> = {};
            const last: Record<string, StudioEvent> = {};
            const nodes: StudioNode[] = [];
            const edges: StudioEdge[] = [];
            for (const event of result.events) {
              if (event.namespace) continue;
              increments[event.node] = (increments[event.node] || 0) + 1;
              last[event.node] = event;
              if (!known.current.has(event.node) && known.current.size < 50) {
                known.current.add(event.node);
                nodes.push({ id: event.node });
              }
              const link = previous.current + '\n' + event.node;
              if (previous.current && previous.current !== event.node && !links.current.has(link) && links.current.size < 150) {
                links.current.add(link);
                edges.push({ source: previous.current, target: event.node });
              }
              previous.current = event.node;
            }
            setCounts((current) => {
              const updated = { ...current };
              for (const [key, value] of Object.entries(increments)) updated[key] = (updated[key] || 0) + value;
              return updated;
            });
            setLatest((current) => ({ ...current, ...last }));
            if (nodes.length || edges.length) setObserved((current) => ({ nodes: [...current.nodes, ...nodes], edges: [...current.edges, ...edges] }));
          }
        }
      } catch (reason) {
        if (!cancelled && (reason as { name?: string })?.name !== 'AbortError') setError(reason instanceof Error ? reason.message : t('graph.lost'));
      } finally {
        busy = false;
      }
    }
    void load();
    const timer = setInterval(() => void load(), active ? 2500 : 15000);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(timer);
    };
  }, [run.id, active, t]);

  const definition = graph?.nodes.length ? graph : observed;
  const labelled = useMemo(() => definition.nodes.map((node) => ({ ...node, label: nodeLabel(node.id, t) })), [definition.nodes, t]);
  const layout = useMemo(() => graphLayout(labelled, definition.edges), [labelled, definition.edges]);
  const lastNode = Object.values(latest).sort((a, b) => b.id - a.id)[0]?.node;
  const selectedNode = layout.nodes.find((node) => node.id === selected);
  const selectedEvent = focusedEvent || latest[selected];
  const points = new Map(layout.nodes.map((node) => [node.id, node]));
  const visibleEvents = events.filter((event) => showMessages || event.kind !== 'message');
  const selectNode = (id: string) => {
    setSelected(id);
    setFocusedEvent(null);
    setDataTab(false);
  };
  function nodeStatus(id: string) {
    if (run.status === 'running' && id === lastNode) return 'active';
    if (counts[id] || (run.status === 'completed' && id === '__end__')) return 'visited';
    return 'idle';
  }
  const nodeIcon = (id: string) => (id === 'model' ? 'spark' : id === 'tools' ? 'layers' : id === '__end__' ? 'check' : id === '__start__' ? 'arrow' : 'shield') as 'spark' | 'layers' | 'check' | 'arrow' | 'shield';

  return (
    <div className={'studio-graph-grid' + (view === 'events' ? ' studio-events-view' : '')} data-studio-graph={run.id}>
      <section className="studio-canvas-panel">
        <div className="studio-canvas-label">
          <span><i className={'studio-dot ' + (error ? 'is-error' : active ? 'is-live' : 'is-ready')} />{view === 'events' ? t('graph.eventLog') : t('graph.flow')}</span>
          {view === 'events' ? (
            <button type="button" className="studio-reconnect" aria-pressed={showMessages} onClick={() => setShowMessages((value) => !value)}>
              {t('graph.messageDetails')}: {showMessages ? t('common.on') : t('common.off')}
            </button>
          ) : (
            <span>{graph?.coverage === 'definition' ? t('graph.topology') : t('graph.observedSteps')}</span>
          )}
        </div>
        {error && <div className="studio-inline-error" role="alert">{error}</div>}
        {view === 'graph' ? (
          <div className="studio-graph-canvas">
            {layout.nodes.length ? (
              <svg
                viewBox={`0 0 ${layout.width} ${layout.height}`}
                role="group"
                aria-label={t('graph.interactive')}
                onPointerDown={(event) => {
                  if ((event.target as Element).closest('[role="button"]')) return;
                  event.currentTarget.setPointerCapture(event.pointerId);
                  drag.current = { x: event.clientX, y: event.clientY, pan };
                }}
                onPointerMove={(event) => {
                  if (!drag.current) return;
                  const rect = event.currentTarget.getBoundingClientRect();
                  setPan({
                    x: drag.current.pan.x + ((event.clientX - drag.current.x) * layout.width) / rect.width,
                    y: drag.current.pan.y + ((event.clientY - drag.current.y) * layout.height) / rect.height,
                  });
                }}
                onPointerUp={() => { drag.current = null; }}
                onPointerCancel={() => { drag.current = null; }}
              >
                <defs>
                  <marker id={marker} markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0 7 3.5 0 7" fill="currentColor" /></marker>
                </defs>
                <g transform={`translate(${pan.x + layout.width / 2} ${pan.y + layout.height / 2}) scale(${zoom}) translate(${-layout.width / 2} ${-layout.height / 2})`}>
                  {definition.edges.map((edge, index) => {
                    const from = points.get(edge.source);
                    const to = points.get(edge.target);
                    if (!from || !to) return null;
                    const back = to.y <= from.y;
                    const x1 = from.x + 90, y1 = from.y + 68, x2 = to.x + 90, y2 = to.y;
                    const path = back
                      ? `M${from.x + 180} ${from.y + 34} C${from.x + 250} ${from.y + 34},${to.x + 250} ${to.y + 34},${to.x + 180} ${to.y + 34}`
                      : `M${x1} ${y1} C${x1} ${y1 + 35},${x2} ${y2 - 35},${x2} ${y2}`;
                    return <path key={index} d={path} className={'studio-graph-edge' + (edge.conditional ? ' conditional' : '')} markerEnd={`url(#${marker})`} />;
                  })}
                  {layout.nodes.map((node) => {
                    const status = nodeStatus(node.id);
                    return (
                      <g
                        key={node.id}
                        role="button"
                        tabIndex={0}
                        aria-label={t('graph.stepLabel', { name: node.label })}
                        aria-pressed={selected === node.id}
                        className={`studio-graph-node ${status}${selected === node.id ? ' selected' : ''}`}
                        transform={`translate(${node.x} ${node.y})`}
                        onClick={() => selectNode(node.id)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            selectNode(node.id);
                          }
                        }}
                      >
                        <rect className="studio-node-glow" x="-5" y="-5" width="190" height="78" rx="17" />
                        <rect className="studio-node-box" width="180" height="68" rx="12" />
                        <rect className="studio-node-icon-box" x="12" y="15" width="36" height="36" rx="10" />
                        <g transform="translate(21 24)"><Icon name={nodeIcon(node.id)} size={18} /></g>
                        <text x="60" y="29" className="studio-node-title">{node.label.length > 19 ? node.label.slice(0, 18) + '…' : node.label}</text>
                        <text x="60" y="47" className="studio-node-caption">
                          {status === 'active' ? t('graph.running') : counts[node.id] ? t('graph.events', { count: counts[node.id] }) : node.id === '__end__' && run.status === 'completed' ? t('graph.finished') : t('graph.node')}
                        </text>
                        <circle cx="164" cy="15" r="3" className="studio-node-status" />
                      </g>
                    );
                  })}
                </g>
              </svg>
            ) : (
              <div className="studio-graph-empty">
                <span className="studio-empty-orbit"><Icon name="graph" size={32} /></span>
                <h3>{active ? t('graph.waitingTitle') : t('graph.noneTitle')}</h3>
                <p>{active ? t('graph.waitingBody') : t('graph.noneBody')}</p>
              </div>
            )}
            <div className="studio-canvas-controls">
              <button onClick={() => setZoom((value) => Math.max(0.55, value - 0.15))} aria-label={t('graph.zoomOut')}><Icon name="minus" /></button>
              <span>{Math.round(zoom * 100)}%</span>
              <button onClick={() => setZoom((value) => Math.min(1.9, value + 0.15))} aria-label={t('graph.zoomIn')}><Icon name="plus" /></button>
              <button onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }} aria-label={t('graph.fit')}><Icon name="fit" /></button>
            </div>
          </div>
        ) : (
          <div className="studio-event-list" role="log" aria-label={t('graph.realEvents')}>
            {visibleEvents.length ? (
              visibleEvents.slice().reverse().map((event) => (
                <button key={event.id} onClick={() => { setSelected(event.node); setFocusedEvent(event); setDataTab(true); }} className={focusedEvent?.id === event.id ? 'selected' : ''}>
                  <span className={'studio-event-symbol ' + event.kind}><Icon name={event.kind === 'interrupt' ? 'shield' : event.kind === 'message' ? 'message' : 'activity'} size={15} /></span>
                  <span>
                    <strong>{event.node}</strong>
                    <small>{defaultSummary(event, t)}</small>
                    {event.namespace && <em>{t('graph.subgraph')} · {event.namespace.split('|').at(-1)?.slice(0, 65)}</em>}
                  </span>
                  <code>#{event.id}</code>
                </button>
              ))
            ) : (
              <div className="studio-small-empty">{t('graph.noEvents')}</div>
            )}
          </div>
        )}
        <div className="studio-canvas-footer">
          <span><i className="studio-legend-dot" />{t('graph.legend.observed')}</span>
          <span><i className="studio-legend-dot cyan" />{t('graph.legend.active')}</span>
          <span>{t('graph.counts', { nodes: layout.nodes.length, edges: definition.edges.length })}</span>
        </div>
      </section>
      <aside className="studio-inspector" aria-label={t('graph.inspectorLabel')}>
        <div className="studio-panel-heading"><span className="studio-kicker">INSPECTOR</span><Icon name="layers" size={15} /></div>
        <h3>{selectedNode?.label || nodeLabel(selected, t)}</h3>
        <code className="studio-node-id">{selected}</code>
        <div className="studio-inspector-tabs">
          <button className={!dataTab ? 'selected' : ''} onClick={() => setDataTab(false)}>{t('graph.overview')}</button>
          <button className={dataTab ? 'selected' : ''} onClick={() => setDataTab(true)}>{t('graph.eventData')}</button>
        </div>
        {dataTab ? (
          <>
            <p>{t('graph.eventDataHint')}</p>
            <pre className="studio-code">{selectedEvent?.detail || t('graph.noEventForNode')}</pre>
          </>
        ) : (
          <>
            <dl className="studio-facts">
              <div><dt>{t('graph.source')}</dt><dd>{graph?.coverage === 'definition' ? t('graph.definition') : t('graph.eventLogShort')}</dd></div>
              <div><dt>{t('graph.observedEvents')}</dt><dd>{counts[selected] || 0}</dd></div>
              <div><dt>{t('graph.lastId')}</dt><dd>{selectedEvent ? '#' + selectedEvent.id : '—'}</dd></div>
              <div><dt>{t('graph.step')}</dt><dd>{selectedEvent?.step ?? '—'}</dd></div>
            </dl>
            <div className="studio-inspector-note">
              <Icon name="activity" size={19} />
              <p>{selectedEvent ? defaultSummary(selectedEvent, t) : t('graph.pickNode')}</p>
            </div>
            <p className="studio-caption">{graph?.coverage === 'definition' ? t('graph.captionDefinition') : t('graph.captionObserved')}</p>
          </>
        )}
      </aside>
    </div>
  );
}
