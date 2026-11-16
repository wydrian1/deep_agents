import { useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { formatDate, kilobytes } from '../lib/format';
import { runsOnWeekday, scheduleLabel, scheduleTime } from '../lib/schedule';
import { AGENT_TEMPLATES, type AgentTemplate } from '../lib/templates';
import { toolIcon, toolLabelById } from '../lib/tools';
import type { Agent, ToolInfo, UploadedFile } from '../types';
import Icon from './Icon';

type Props = {
  agents: Agent[];
  tools: ToolInfo[];
  files: UploadedFile[];
  timeZone: string;
  busy: boolean;
  canStart: boolean;
  runIds: string[];
  onRun: (id: string) => void;
  onEdit: (agent: Agent) => void;
  onToggle: (agent: Agent) => void;
  onDelete: (agent: Agent) => void;
  onLastRun: (id: string) => void;
  onNew: () => void;
  onTemplate: (template: AgentTemplate) => void;
  scheduled?: boolean;
};

export default function AgentLibrary({ agents, tools, files, timeZone, busy, canStart, runIds, onRun, onEdit, onToggle, onDelete, onLastRun, onNew, onTemplate, scheduled = false }: Props) {
  const { t, lang } = useI18n();
  const [search, setSearch] = useState('');
  const [day, setDay] = useState<number | null>(null);
  const [inspected, setInspected] = useState<Agent | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const toolShort = (name: string) => toolLabelById(name, tools, t);
  const lower = (value: string) => value.toLocaleLowerCase(lang);
  const entries = agents
    .filter((agent) => (!scheduled || agent.schedule) && (!search || lower(agent.name + ' ' + agent.config.prompt).includes(lower(search))) && (day === null || !scheduled || runsOnWeekday(agent.schedule, day)))
    .sort((a, b) =>
      scheduled
        ? Number(b.enabled) - Number(a.enabled) || new Date(a.next_run ?? '9999').getTime() - new Date(b.next_run ?? '9999').getTime()
        : a.name.localeCompare(b.name, lang),
    );
  const fileInfo = (id: string) => files.find((file) => file.id === id);
  function inspect(agent: Agent) {
    setInspected(agent);
    requestAnimationFrame(() => dialog.current?.showModal());
  }
  const modeLabel = (agent: Agent) => (agent.config.mode === 'test' ? t('mode.test') : agent.config.model || t('mode.liveShort'));

  return (
    <section className="studio-library">
      <div className="studio-section-heading">
        <div>
          <span className="studio-kicker">{scheduled ? t('library.kickerSchedules') : t('library.kicker')}</span>
          <h2>{scheduled ? t('nav.schedules') : t('nav.library')}</h2>
          <p>{scheduled ? t('library.schedulesHint', { zone: timeZone }) : t('library.hint')}</p>
        </div>
        <button className="studio-button primary" onClick={onNew}><Icon name="plus" />{t('library.newAgent')}</button>
      </div>
      {!scheduled && (
        <details className="agent-templates">
          <summary><Icon name="spark" size={15} />{t('templates.title')}</summary>
          <ul>
            {AGENT_TEMPLATES.map((item) => (
              <li key={item.id}>
                <div><strong>{t(`templates.${item.id}.name`)}</strong><p>{t(`templates.${item.id}.summary`)}</p></div>
                <button type="button" className="studio-button" disabled={busy} onClick={() => onTemplate(item)}>{t('templates.use')}</button>
              </li>
            ))}
          </ul>
          <small>{t('templates.note')}</small>
        </details>
      )}
      {scheduled && (
        <div className="studio-week">
          <div className="studio-week-heading">
            <span><Icon name="calendar" />{t('week.title')}</span>
            <small>{t('week.rules', { zone: timeZone })}</small>
            {day !== null && <button onClick={() => setDay(null)}>{t('week.allDays')}</button>}
          </div>
          <div className="studio-week-days">
            {Array.from({ length: 7 }, (_, index) => {
              const items = agents.filter((agent) => agent.enabled && agent.schedule && runsOnWeekday(agent.schedule, index));
              return (
                <div key={index} className={day === index ? 'selected' : ''}>
                  <button className="studio-day-label" aria-pressed={day === index} onClick={() => setDay((current) => (current === index ? null : index))}>
                    {t('weekdayShort.' + index)}<span>{t('weekday.' + index)}</span>
                  </button>
                  <div className="studio-day-items">
                    {items.slice(0, 3).map((agent) => (
                      <button key={agent.id} title={agent.name} aria-label={`${agent.name}, ${scheduleTime(agent.schedule!)}`} onClick={() => inspect(agent)}>
                        <span>{scheduleTime(agent.schedule!)}</span>{agent.name}
                      </button>
                    ))}
                    {items.length > 3 && <small>{t('week.more', { count: items.length - 3 })}</small>}
                    {!items.length && <span className="studio-day-empty">—</span>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
      <div className="studio-library-toolbar">
        <label className="studio-search">
          <Icon name="search" />
          <input aria-label={scheduled ? t('library.searchSchedules') : t('library.searchAgents')} value={search} onChange={(event) => setSearch(event.target.value)} placeholder={scheduled ? t('library.searchSchedulesPh') : t('library.searchAgentsPh')} />
        </label>
        <span>{entries.length} {scheduled ? t('library.countSchedules') : t('library.countAgents')}</span>
      </div>
      <div className={'studio-agent-grid' + (scheduled ? ' schedule-grid' : '')}>
        {entries.map((agent) => (
          <article className="studio-agent-tile" key={agent.id} data-saved-agent={agent.id}>
            <div className="studio-agent-tile-top">
              <span className={'studio-avatar ' + (agent.config.tools.includes('github') ? 'violet' : agent.config.tools.includes('report') ? 'amber' : 'mint')}>
                <Icon name={scheduled ? 'calendar' : agent.config.tools.length ? toolIcon(agent.config.tools[0]) : 'spark'} size={23} />
              </span>
              <span className={'studio-badge ' + (agent.enabled ? 'mint' : 'muted')}>{agent.enabled ? t('library.auto') : !agent.schedule ? t('library.manualBadge') : t('library.paused')}</span>
              <button className="studio-icon-button" onClick={() => inspect(agent)} aria-label={t('library.details', { name: agent.name })}><Icon name="more" /></button>
            </div>
            <button className="studio-agent-title" onClick={() => inspect(agent)}><h3>{agent.name}</h3></button>
            <p className="studio-agent-description">{agent.config.prompt}</p>
            <div className="studio-tag-row">
              <span className="studio-tag">{modeLabel(agent)}</span>
              {agent.config.tools.map((tool) => (
                <span key={tool} className="studio-tool-tag" title={toolShort(tool)}><Icon name={toolIcon(tool)} size={13} />{toolShort(tool)}</span>
              ))}
              {!!agent.config.files.length && <span className="studio-tag"><Icon name="file" size={12} />{t('library.fileCount', { count: agent.config.files.length })}</span>}
            </div>
            <div className="studio-agent-next">
              <Icon name="clock" size={16} />
              <div>
                <strong>{scheduleLabel(agent.schedule, t)}</strong>
                <span>{agent.enabled && agent.next_run ? t('library.next', { time: formatDate(agent.next_run, timeZone, lang) }) : !agent.schedule ? t('library.startAnytime') : t('library.autoPaused')}</span>
              </div>
              {agent.schedule && (
                <button className={'studio-switch' + (agent.enabled ? ' on' : '')} role="switch" aria-checked={agent.enabled} aria-label={agent.enabled ? t('library.pause') : t('library.enable')} disabled={busy} onClick={() => onToggle(agent)}><span /></button>
              )}
            </div>
            {agent.last_error && <p className="studio-inline-error" role="alert">{agent.last_error}</p>}
            <div className="studio-agent-tile-actions">
              <button className="studio-button" disabled={busy || !canStart} onClick={() => onRun(agent.id)} aria-label={t('library.runAria', { name: agent.name })}><Icon name="play" size={14} />{t('library.run')}</button>
              <button className="studio-button" disabled={busy} onClick={() => onEdit(agent)} aria-label={t('library.editAria', { name: agent.name })}>{t('library.edit')}</button>
              {agent.last_run_id && (
                <button className="studio-icon-button" disabled={!runIds.includes(agent.last_run_id)} title={!runIds.includes(agent.last_run_id) ? t('library.lastRunMissing') : t('library.lastRun')} aria-label={t('library.lastRunAria', { name: agent.name })} onClick={() => onLastRun(agent.last_run_id!)}><Icon name="activity" size={17} /></button>
              )}
              <button className="studio-icon-button danger" disabled={busy} aria-label={t('library.deleteAria', { name: agent.name })} onClick={() => onDelete(agent)}><Icon name="trash" size={16} /></button>
            </div>
          </article>
        ))}
      </div>
      {!entries.length && (
        <div className="studio-empty-state">
          <span className="studio-empty-orbit"><Icon name={scheduled ? 'calendar' : 'layers'} size={30} /></span>
          <h3>{search || day !== null ? t('library.noMatch') : scheduled ? t('library.emptySchedulesTitle') : t('library.emptyTitle')}</h3>
          <p>{scheduled ? t('library.emptySchedulesBody') : t('library.emptyBody')}</p>
          <button className="studio-button primary" onClick={onNew}><Icon name="plus" />{t('library.createAgent')}</button>
        </div>
      )}
      <dialog ref={dialog} className="studio-dialog studio-detail-dialog" aria-labelledby="saved-agent-title">
        <div className="studio-dialog-heading">
          <div><span className="studio-kicker">{t('library.savedConfig')}</span><h2 id="saved-agent-title">{inspected?.name || t('library.agentDetails')}</h2></div>
          <button className="studio-icon-button" onClick={() => dialog.current?.close()} aria-label={t('common.close')}><Icon name="close" /></button>
        </div>
        {inspected && (
          <div className="studio-dialog-body">
            <div className="studio-tag-row">
              <span className="studio-badge mint">{inspected.config.mode === 'test' ? t('mode.test') : inspected.config.model || t('mode.liveShort')}</span>
              <span className="studio-tag">{scheduleLabel(inspected.schedule, t)}</span>
            </div>
            <h3>{t('library.savedTask')}</h3>
            <pre className="studio-prompt-preview">{inspected.config.prompt}</pre>
            <h3>{t('library.allowedTools')}</h3>
            <div className="studio-tag-row">
              {inspected.config.tools.map((tool) => <span key={tool} className="studio-tag">{toolShort(tool)}</span>)}
              {!inspected.config.tools.length && <p>{t('library.noTools')}</p>}
            </div>
            <h3>{t('library.attached')}</h3>
            {inspected.config.files.map((id) => {
              const file = fileInfo(id);
              return <div className="studio-attached-file" key={id}><Icon name="file" /><span>{file?.name ?? id.slice(0, 8)}{file && <small>{kilobytes(file.size)} KB</small>}</span></div>;
            })}
            {!inspected.config.files.length && <p>{t('library.noFiles')}</p>}
            <p>{t('library.eachRun')}</p>
            <div className="studio-actions">
              <button className="studio-button" disabled={busy || !agents.some((agent) => agent.id === inspected.id)} onClick={() => { const current = agents.find((agent) => agent.id === inspected.id); if (current) { dialog.current?.close(); onEdit(current); } }}>{t('library.editAgent')}</button>
              <button className="studio-button primary" disabled={busy || !canStart} onClick={() => { dialog.current?.close(); onRun(inspected.id); }}><Icon name="play" />{t('library.runAgent')}</button>
            </div>
          </div>
        )}
      </dialog>
    </section>
  );
}
