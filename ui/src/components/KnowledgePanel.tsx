import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import { useI18n } from '../i18n';
import type { MemoryEntry, MemorySummary } from '../types';
import Icon from './Icon';

const KEY = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const MAX_CHARS = 12000;
const newRequestId = () => 'ui-' + (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`);

type Props = { formatDate: (value: string) => string };

/** The shared private knowledge base: versioned text entries that agents with the read/write grants share. */
export default function KnowledgePanel({ formatDate }: Props) {
  const { t } = useI18n();
  const [prefix, setPrefix] = useState('');
  const [deleted, setDeleted] = useState(false);
  const [items, setItems] = useState<MemorySummary[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [selected, setSelected] = useState<MemoryEntry | null>(null);
  const [creating, setCreating] = useState(false);
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [history, setHistory] = useState<MemoryEntry[]>([]);
  const [historyNext, setHistoryNext] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState<number | null>(null);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const requestId = useRef(newRequestId());

  const fail = useCallback((reason: unknown) => setError(reason instanceof Error ? reason.message : t('kb.errGeneric')), [t]);
  const loadList = useCallback(async (after = '') => {
    try {
      const page = await api.memoryList(prefix.trim().toLowerCase(), deleted, after);
      setItems((current) => (after ? [...current, ...page.entries] : page.entries));
      setNext(page.next);
      setError('');
    } catch (reason) {
      fail(reason);
    }
  }, [prefix, deleted, fail]);
  useEffect(() => { void loadList(); }, [loadList]);

  const open = useCallback(async (name: string) => {
    try {
      const [{ entry }, past] = await Promise.all([api.memoryGet(name), api.memoryHistory(name)]);
      setCreating(false);
      setSelected(entry);
      setKey(name);
      setValue(entry?.deleted ? '' : entry?.value ?? '');
      setHistory(past.versions);
      setHistoryNext(past.next);
      setConflict(null);
      setError('');
      setNotice('');
      requestId.current = newRequestId();
    } catch (reason) {
      fail(reason);
    }
  }, [fail]);

  function startNew() {
    setCreating(true);
    setSelected(null);
    setKey('');
    setValue('');
    setHistory([]);
    setHistoryNext(null);
    setConflict(null);
    setError('');
    setNotice('');
    requestId.current = newRequestId();
  }

  async function save() {
    const expected = creating ? 0 : selected?.revision ?? 0;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await api.memoryPut(key, value, expected, requestId.current);
      setNotice(t('kb.saved'));
      await Promise.all([open(key), loadList()]);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) setConflict(reason.current_revision);
      fail(reason);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!selected || !window.confirm(t('kb.confirmDelete', { key: selected.key }))) return;
    setBusy(true);
    setError('');
    try {
      await api.memoryDelete(selected.key, selected.revision, newRequestId());
      setNotice(t('kb.deletedNotice'));
      await Promise.all([open(selected.key), loadList()]);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) setConflict(reason.current_revision);
      fail(reason);
    } finally {
      setBusy(false);
    }
  }

  async function moreHistory() {
    if (!selected || historyNext === null) return;
    try {
      const page = await api.memoryHistory(selected.key, historyNext);
      setHistory((current) => [...current, ...page.versions]);
      setHistoryNext(page.next);
    } catch (reason) {
      fail(reason);
    }
  }

  const keyValid = KEY.test(key);
  const canSave = !busy && keyValid && value.length <= MAX_CHARS && (creating || !!selected) && !(selected?.deleted && !value.trim() && !creating);
  const dirty = creating ? value.length > 0 : selected ? (selected.deleted ? value.length > 0 : value !== selected.value) : false;

  return (
    <section className="memory-panel studio-library">
      <div className="studio-section-heading">
        <div>
          <span className="studio-kicker">{t('kb.kicker')}</span>
          <h2>{t('nav.knowledge')}</h2>
          <p>{t('kb.hint')}</p>
        </div>
        <button className="studio-button primary" onClick={startNew}><Icon name="plus" />{t('kb.new')}</button>
      </div>
      <p className="memory-warning"><Icon name="shield" size={14} /> {t('kb.warning')}</p>
      {error && <div className="studio-notice error" role="alert"><Icon name="shield" />{error}{conflict !== null && selected && <button type="button" className="studio-button" onClick={() => void open(selected.key)}>{t('kb.reload')}</button>}</div>}
      {notice && <div className="studio-notice" role="status"><Icon name="check" />{notice}</div>}
      <div className="memory-layout">
        <div className="memory-list">
          <label>{t('kb.prefix')}<input aria-label={t('kb.prefix')} value={prefix} maxLength={80} onChange={(event) => setPrefix(event.target.value)} placeholder="notes." /></label>
          <label className="memory-check"><input type="checkbox" checked={deleted} onChange={(event) => setDeleted(event.target.checked)} />{t('kb.showDeleted')}</label>
          {items.map((item) => (
            <button key={item.key} type="button" className={'memory-item' + (selected?.key === item.key ? ' selected' : '')} aria-pressed={selected?.key === item.key} onClick={() => void open(item.key)}>
              <strong>{item.key}</strong>
              <small>{item.deleted ? t('kb.deleted') + ' · ' : ''}{t('kb.revision', { revision: item.revision })} · {t('kb.chars', { count: item.characters })}</small>
              <small>{formatDate(item.updated)} · {item.writer}</small>
            </button>
          ))}
          {!items.length && <p><small>{t('kb.empty')}</small></p>}
          {next && <button type="button" className="studio-button" onClick={() => void loadList(next)}>{t('kb.more')}</button>}
        </div>
        <div className="memory-editor">
          {!creating && !selected ? <p>{t('kb.pick')}</p> : (
            <>
              <label>{t('kb.name')}
                <input aria-label={t('kb.name')} value={key} readOnly={!creating} maxLength={80} onChange={(event) => setKey(event.target.value)} placeholder="notes.project" aria-invalid={creating && !!key && !keyValid} />
                {creating && <small>{t('kb.nameRule')}</small>}
              </label>
              {selected && <small>{selected.deleted ? t('kb.deletedEntry') + ' · ' : ''}{t('kb.revision', { revision: selected.revision })} · {formatDate(selected.updated)} · {selected.writer}</small>}
              <label>{t('kb.text')}
                <textarea aria-label={t('kb.text')} rows={12} value={value} maxLength={MAX_CHARS} onChange={(event) => setValue(event.target.value)} />
                <small>{value.length} / {MAX_CHARS}</small>
              </label>
              <div className="studio-actions">
                <button type="button" className="studio-button primary" disabled={!canSave || !dirty} onClick={() => void save()}><Icon name="save" size={15} />{t('kb.save')}</button>
                {selected && !selected.deleted && <button type="button" className="studio-button danger" disabled={busy} onClick={() => void remove()}><Icon name="trash" size={15} />{t('kb.delete')}</button>}
              </div>
              {selected && (
                <details>
                  <summary>{t('kb.history', { count: history.length })}</summary>
                  {history.map((version) => (
                    <details key={version.revision}>
                      <summary>{t('kb.revision', { revision: version.revision })} · {formatDate(version.updated)} · {version.writer}{version.deleted ? ' · ' + t('kb.deleted') : ''}</summary>
                      <pre>{version.deleted ? t('kb.deletedEntry') : version.value}</pre>
                      {!version.deleted && <button type="button" className="studio-button" onClick={() => { setValue(version.value); setNotice(t('kb.restoreHint', { revision: version.revision })); }}>{t('kb.restore')}</button>}
                    </details>
                  ))}
                  {historyNext !== null && <button type="button" className="studio-button" onClick={() => void moreHistory()}>{t('kb.more')}</button>}
                </details>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
