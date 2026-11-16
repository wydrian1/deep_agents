import { forwardRef, useImperativeHandle, useRef, useState } from 'react';
import { api, type RunInput, type ScheduleInput } from '../api';
import { useI18n } from '../i18n';
import { kilobytes, runTitle } from '../lib/format';
import { clock, scheduleSummary, type T } from '../lib/schedule';
import type { AgentTemplate } from '../lib/templates';
import { toggleTool, toolIcon, toolText } from '../lib/tools';
import type { Agent, Run, Schedule, ServiceStatus, ToolInfo, UploadedFile } from '../types';
import Icon from './Icon';

type Frequency = 'none' | 'hourly' | 'daily' | 'weekly';
type Draft = { frequency: Frequency; hour: number; minute: number; weekday: number };
const newDraft = (): Draft => ({ frequency: 'none', hour: 9, minute: 0, weekday: 0 });

export type ComposerResult = { kind: 'run'; run: Run } | { kind: 'agent'; agent: Agent; editing: boolean };
export type ComposerHandle = {
  open: (kind: 'run' | 'agent', options?: { from?: Run; edit?: Agent; template?: AgentTemplate }) => void;
};
type Props = {
  tools: ToolInfo[];
  files: UploadedFile[];
  status: ServiceStatus | null;
  active: number;
  onDone: (result: ComposerResult) => void;
  onFilesChanged: () => void;
};

const MAX_FILES = 10, MAX_FILE = 2 * 1024 * 1024, MAX_TOTAL = 10 * 1024 * 1024;

function toSchedule(draft: Draft): Schedule | null {
  if (draft.frequency === 'none') return null;
  if (draft.frequency === 'hourly') return { kind: 'hourly', minute: draft.minute };
  if (draft.frequency === 'daily') return { kind: 'daily', hour: draft.hour, minute: draft.minute };
  return { kind: 'weekly', hour: draft.hour, minute: draft.minute, weekday: draft.weekday };
}
const toDraft = (schedule: Schedule | null): Draft =>
  schedule ? { frequency: schedule.kind, hour: schedule.hour ?? 9, minute: schedule.minute, weekday: schedule.weekday ?? 0 } : newDraft();

const Composer = forwardRef<ComposerHandle, Props>(function Composer({ tools: catalog, files, status, active, onDone, onFilesChanged }, ref) {
  const { t, lang } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  const [composerMode, setComposerMode] = useState<'run' | 'agent'>('run');
  const [sourceRun, setSourceRun] = useState<Run | null>(null);
  const [editing, setEditing] = useState<Agent | null>(null);
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [mode, setMode] = useState<'test' | 'live'>('test');
  const [model, setModel] = useState('');
  const [tools, setTools] = useState<string[]>([]);
  const [fileIds, setFileIds] = useState<string[]>([]);
  const [draft, setDraft] = useState<Draft>(newDraft);
  const [consent, setConsent] = useState(false);
  const [editEnabled, setEditEnabled] = useState(false);
  const [fileSearch, setFileSearch] = useState('');
  const [uploading, setUploading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const maxPrompt = status?.max_prompt_chars ?? 4000;
  const max = status?.max_concurrent_runs ?? 3;
  const locked = !!sourceRun;
  const schedule = toSchedule(draft);
  const consentNeeded = !!schedule && (editing ? editEnabled : true);
  const baseOk = !!prompt.trim() && prompt.length <= maxPrompt && !busy && !uploading;
  const canStart = baseOk && active < max;
  const canSave = !busy && !uploading && !!name.trim() && (locked || baseOk) && (!consentNeeded || consent);

  useImperativeHandle(ref, () => ({
    open(kind, options) {
      setError('');
      setComposerMode(kind);
      setConsent(false);
      setFileSearch('');
      const from = options?.from, edit = options?.edit, template = options?.template;
      setSourceRun(from ?? null);
      setEditing(edit ?? null);
      if (edit) {
        setName(edit.name); setPrompt(edit.config.prompt); setMode(edit.config.mode); setModel(edit.config.model ?? '');
        setTools([...edit.config.tools]); setFileIds([...edit.config.files]); setDraft(toDraft(edit.schedule)); setEditEnabled(edit.enabled);
      } else if (from) {
        setName(runTitle(from.prompt, t).slice(0, 100)); setPrompt(from.prompt); setMode(from.mode); setModel(from.mode === 'live' ? from.model : '');
        setTools([...from.tools]); setFileIds([...(from.files ?? [])]); setDraft(newDraft()); setEditEnabled(false);
      } else if (template) {
        setName(t(`templates.${template.id}.name`)); setPrompt(t(`templates.${template.id}.prompt`)); setMode('test'); setModel('');
        setTools([...template.tools]); setFileIds([]); setDraft(newDraft()); setEditEnabled(false);
      } else {
        setName(''); setPrompt(''); setMode('test'); setModel(''); setTools([]); setFileIds([]); setDraft(newDraft()); setEditEnabled(false);
      }
      dialog.current?.showModal();
      requestAnimationFrame(() => dialog.current?.querySelector<HTMLInputElement | HTMLTextAreaElement>(kind === 'agent' ? 'input[data-field="name"]' : 'textarea')?.focus());
    },
  }));

  const ensureFilesTool = (current: string[]) => (current.includes('files') ? current : toggleTool(current, 'files', catalog));
  function toggle(id: string) {
    setTools((current) => toggleTool(current, id, catalog));
    if (id === 'files' && tools.includes('files')) setFileIds([]);
  }
  function toggleFile(id: string) {
    setFileIds((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
    setTools(ensureFilesTool);
  }
  const selectedSize = () => files.filter((file) => fileIds.includes(file.id)).reduce((sum, file) => sum + file.size, 0);

  async function upload(list: FileList | null) {
    if (!list || locked) return;
    setUploading(true);
    setError('');
    try {
      const incoming = Array.from(list);
      if (fileIds.length + incoming.length > MAX_FILES) throw new Error(t('composer.errMaxFiles', { count: MAX_FILES }));
      if (incoming.reduce((sum, file) => sum + file.size, selectedSize()) > MAX_TOTAL) throw new Error(t('composer.errTotal'));
      for (const file of incoming) {
        if (file.size > MAX_FILE) throw new Error(t('composer.errFileSize'));
        const saved = await api.upload(file);
        setFileIds((current) => (current.length < MAX_FILES ? [...current, saved.id] : current));
        setTools(ensureFilesTool);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('composer.errUpload'));
    } finally {
      setUploading(false);
      onFilesChanged();
    }
  }

  function config(): RunInput {
    return {
      prompt: prompt.trim(), mode, model: mode === 'live' ? model.trim() || null : null, tools, files: fileIds,
      language: editing?.config.language ?? lang,
    };
  }
  const scheduleInput = (): ScheduleInput => ({ schedule, enabled: !!schedule && (editing ? editEnabled : true), confirm_recurring_cost: consent });

  async function perform(work: () => Promise<ComposerResult>) {
    setBusy(true);
    setError('');
    try {
      const result = await work();
      dialog.current?.close();
      onDone(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('composer.errGeneric'));
    } finally {
      setBusy(false);
    }
  }
  const start = () => void perform(async () => ({ kind: 'run', run: await api.startRun(config()) }));
  function save() {
    if (!canSave) return;
    void perform(async () => {
      const agent = sourceRun
        ? await api.agentFromRun(sourceRun.id, name.trim(), scheduleInput())
        : editing
          ? await api.updateAgent(editing.id, editing.revision, name.trim(), config(), scheduleInput())
          : await api.createAgent(name.trim(), config(), scheduleInput());
      return { kind: 'agent', agent, editing: !!editing };
    });
  }
  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (composerMode === 'agent' || sourceRun) save();
    else if (canStart) start();
  }

  const heading = editing ? t('composer.editTitle') : sourceRun ? t('composer.fromRunTitle') : composerMode === 'agent' ? t('composer.newAgent') : t('composer.newTask');
  const kicker = editing ? t('composer.kickerEdit') : sourceRun ? t('composer.kickerFromRun') : t('composer.kickerNew');
  const intro = editing ? t('composer.introEdit') : sourceRun ? t('composer.introFromRun') : t('composer.intro');
  const accessLabel = (tool: ToolInfo, tt: T) => (tool.access === 'read' ? tt('tool.readOnly') : tt('tool.privateWrite'));
  const shownFiles = files.filter((file) => file.name.toLocaleLowerCase(lang).includes(fileSearch.toLocaleLowerCase(lang)));

  return (
    <dialog ref={dialog} className="studio-dialog studio-composer" aria-labelledby="agent-composer-title">
      <form onSubmit={submit}>
        <div className="studio-dialog-heading">
          <div>
            <span className="studio-kicker">{kicker}</span>
            <h2 id="agent-composer-title">{heading}</h2>
            <p>{intro}</p>
          </div>
          <button type="button" className="studio-icon-button" onClick={() => dialog.current?.close()} aria-label={t('common.close')}><Icon name="close" /></button>
        </div>
        <div className="studio-dialog-body">
          {error && <div className="studio-notice error" role="alert"><Icon name="shield" />{error}</div>}
          <div className="studio-form-row">
            <label>
              {t('composer.agentName')} <span className="studio-optional">{t('composer.forSaving')}</span>
              <input data-field="name" aria-label={t('composer.agentName')} value={name} maxLength={100} onChange={(event) => setName(event.target.value)} placeholder={t('composer.namePh')} />
            </label>
            <label>
              {t('composer.modelMode')}
              <select aria-label={t('composer.modelMode')} value={mode} disabled={locked} onChange={(event) => setMode(event.target.value as 'test' | 'live')}>
                <option value="test">{t('mode.testLong')}</option>
                <option value="live">{t('mode.liveLong')}</option>
              </select>
              <small>{mode === 'test' ? t('composer.testHint') : status?.model ? t('composer.liveHint', { model: status.model }) : t('composer.liveNoDefault')}</small>
            </label>
          </div>
          {mode === 'live' && (
            <label className="studio-select-model">
              {t('composer.modelName')} <span className="studio-optional">{t('composer.optional')}</span>
              <input aria-label={t('composer.modelName')} value={model} readOnly={locked} maxLength={200} onChange={(event) => setModel(event.target.value)} placeholder={status?.model || t('composer.modelPh')} />
              <small>{t('composer.modelAlias')}</small>
            </label>
          )}
          <label className="studio-prompt-label">
            {t('composer.prompt')}
            <textarea required maxLength={maxPrompt} rows={5} value={prompt} readOnly={locked} onChange={(event) => setPrompt(event.target.value)} placeholder={t('composer.promptPh')} />
            <span>{prompt.length} / {maxPrompt}</span>
          </label>
          <fieldset className="studio-tool-fieldset">
            <legend><span>{t('composer.tools')}</span><small>{t('composer.selected', { count: tools.length })}</small></legend>
            <div className="studio-tool-grid">
              {catalog.map((tool) => (
                <label key={tool.name} className={'studio-tool-choice' + (tools.includes(tool.name) ? ' checked' : '')} title={toolText(tool, 'description', t)}>
                  <input type="checkbox" checked={tools.includes(tool.name)} disabled={locked} onChange={() => toggle(tool.name)} aria-label={toolText(tool, 'label', t)} />
                  <Icon name={toolIcon(tool.name)} size={21} />
                  <strong>{toolText(tool, 'label', t)}</strong>
                  <small>{accessLabel(tool, t)}</small>
                </label>
              ))}
            </div>
            <p>{t('composer.toolsHint')}</p>
            <div className="studio-tool-policies">
              {catalog.filter((tool) => tools.includes(tool.name)).map((tool) => <p key={tool.name}><strong>{toolText(tool, 'label', t)}: </strong>{toolText(tool, 'description', t)}</p>)}
            </div>
          </fieldset>
          <details className="studio-form-disclosure" open={locked ? true : undefined}>
            <summary>
              <span><Icon name="file" size={17} />{t('composer.attachments')}<span className="studio-count">{fileIds.length}</span></span>
              <span>{t('composer.uploads')} <Icon name="chevron" size={14} /></span>
            </summary>
            <div className="studio-disclosure-body">
              {locked ? (
                <>
                  {fileIds.map((id) => <div className="studio-attached-file" key={id}><Icon name="file" />{files.find((file) => file.id === id)?.name ?? id.slice(0, 8)}</div>)}
                  <p>{t('composer.sourceFilesNote')}</p>
                </>
              ) : (
                <>
                  <label className={'studio-upload' + (uploading ? ' uploading' : '')} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); if (!uploading) void upload(event.dataTransfer.files); }}>
                    <Icon name="upload" size={25} />
                    <strong>{uploading ? t('composer.uploading') : t('composer.dropFiles')}</strong>
                    <span>{t('composer.fileLimits')}</span>
                    <input type="file" multiple aria-label={t('composer.uploadFile')} disabled={uploading} onChange={(event) => { void upload(event.target.files); event.target.value = ''; }} />
                  </label>
                  <div className="studio-file-tools">
                    <label className="studio-search"><Icon name="search" size={14} /><input aria-label={t('composer.searchFiles')} value={fileSearch} onChange={(event) => setFileSearch(event.target.value)} placeholder={t('composer.searchFilesPh')} /></label>
                    <button type="button" className="studio-icon-button" aria-label={t('composer.refreshFiles')} onClick={onFilesChanged}><Icon name="refresh" size={16} /></button>
                  </div>
                  <div className="studio-file-list">
                    {shownFiles.map((file) => (
                      <label key={file.id} className={fileIds.includes(file.id) ? 'selected' : ''}>
                        <input type="checkbox" checked={fileIds.includes(file.id)} disabled={file.size > MAX_FILE || (!fileIds.includes(file.id) && fileIds.length >= MAX_FILES)} onChange={() => toggleFile(file.id)} />
                        <Icon name="file" size={17} />
                        <span>{file.name}<small>{kilobytes(file.size)} KB</small></span>
                      </label>
                    ))}
                    {!files.length && <p>{t('composer.noFiles')}</p>}
                  </div>
                  <p className="studio-caption">{t('composer.filesCaption')}</p>
                </>
              )}
            </div>
          </details>
          <details className="studio-form-disclosure">
            <summary>
              <span><Icon name="calendar" size={17} />{t('composer.schedule')}</span>
              <span>{scheduleSummary(schedule, t)}<Icon name="chevron" size={14} /></span>
            </summary>
            <div className="studio-disclosure-body">
              {editing && schedule && (
                <label className="studio-consent">
                  <input type="checkbox" checked={editEnabled} onChange={(event) => { setEditEnabled(event.target.checked); setConsent(false); }} />
                  <span>{t('composer.scheduleActive')}</span>
                </label>
              )}
              <div className="studio-form-row">
                <label>
                  {t('composer.repeat')}
                  <select value={draft.frequency} onChange={(event) => { setDraft({ ...draft, frequency: event.target.value as Frequency }); setConsent(false); }}>
                    <option value="none">{t('composer.manualOnly')}</option>
                    <option value="hourly">{t('composer.hourly')}</option>
                    <option value="daily">{t('composer.daily')}</option>
                    <option value="weekly">{t('composer.weekly')}</option>
                  </select>
                </label>
                {draft.frequency === 'hourly' && (
                  <label>
                    {t('composer.minute', { zone: status?.schedule_timezone ?? 'UTC' })}
                    <input type="number" min={0} max={59} step={1} value={draft.minute} onChange={(event) => { const minute = Number(event.target.value); if (event.target.value !== '' && Number.isInteger(minute) && minute >= 0 && minute <= 59) setDraft({ ...draft, minute }); }} />
                  </label>
                )}
                {(draft.frequency === 'daily' || draft.frequency === 'weekly') && (
                  <label>
                    {t('composer.time', { zone: status?.schedule_timezone ?? 'UTC' })}
                    <input type="time" value={clock(draft)} onChange={(event) => { const [hour, minute] = event.target.value.split(':').map(Number); if (Number.isInteger(hour) && Number.isInteger(minute)) setDraft({ ...draft, hour, minute }); }} />
                  </label>
                )}
              </div>
              {draft.frequency === 'weekly' && (
                <label>
                  {t('composer.weekday')}
                  <select value={draft.weekday} onChange={(event) => setDraft({ ...draft, weekday: Number(event.target.value) })}>
                    {Array.from({ length: 7 }, (_, index) => <option value={index} key={index}>{t('weekday.' + index)}</option>)}
                  </select>
                </label>
              )}
              {consentNeeded && (
                <label className="studio-consent">
                  <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
                  <span>
                    <strong>{t('composer.consentTitle')}</strong>
                    {t('composer.consentBody')}
                    {draft.frequency === 'hourly' && <em className="studio-schedule-warning"> {t('composer.hourlyWarning')}</em>}
                  </span>
                </label>
              )}
              <p className="studio-caption">{t('composer.scheduleCaption', { zone: status?.schedule_timezone ?? 'UTC' })}</p>
            </div>
          </details>
        </div>
        <div className="studio-dialog-footer">
          <span><Icon name="shield" size={14} />{t('composer.capacity', { active, max, minutes: Math.round((status?.run_timeout_seconds ?? 3600) / 60) })}</span>
          <div>
            <button type="button" className={'studio-button' + (composerMode === 'agent' ? ' primary' : '')} disabled={!canSave} onClick={save}><Icon name="save" size={16} />{t('composer.saveAgent')}</button>
            {!sourceRun && composerMode === 'run' && <button type="submit" className="studio-button primary" disabled={!canStart}><Icon name="play" size={15} />{busy ? t('composer.starting') : t('composer.startTask')}</button>}
          </div>
          <small>{t('composer.footerNote')}</small>
        </div>
      </form>
    </dialog>
  );
});
export default Composer;
