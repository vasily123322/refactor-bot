import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { StudioApiError, studioApi, type CreateSourceInput } from './api';
import { SourceSettingsControls } from './SourceSettingsControls';
import { updateSourceSettings } from './sourceSettingsApi';
import type { SourceSettingsPatch } from './sourceSettingsApi';
import type { Channel, SourceConnectorView } from './types';

function errorMessage(error: unknown): string {
  if (error instanceof StudioApiError || error instanceof Error) return error.message;
  return 'Неизвестная ошибка';
}

function dateLabel(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('ru', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function sourceKindLabel(kind: string): string {
  if (kind === 'rss') return 'RSS';
  if (kind === 'url' || kind === 'web') return 'Web';
  if (kind === 'telegram') return 'Telegram';
  return kind;
}

export function SourcesPanel({ channel }: { channel: Channel | null }) {
  const [sources, setSources] = useState<SourceConnectorView[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [kind, setKind] = useState<CreateSourceInput['kind']>('rss');
  const [value, setValue] = useState('');
  const [mode, setMode] = useState<CreateSourceInput['mode']>('summary');
  const [reusePolicy, setReusePolicy] = useState<CreateSourceInput['reuse_policy']>('reference_only');
  const [citationEnabled, setCitationEnabled] = useState(true);

  const load = useCallback(async () => {
    if (!channel) {
      setSources([]);
      return;
    }
    setSources(await studioApi.sources(channel.id));
  }, [channel]);

  useEffect(() => {
    setError(null);
    setNotice(null);
    void load().catch((reason) => setError(errorMessage(reason)));
  }, [load]);

  const run = async (key: string, action: () => Promise<void>) => {
    setBusyId(key);
    setError(null);
    try {
      await action();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusyId(null);
    }
  };

  const createSource = async (event: FormEvent) => {
    event.preventDefault();
    if (!channel || !value.trim()) return;
    await run('create', async () => {
      const created = await studioApi.createSource(channel.id, {
        kind,
        value: value.trim(),
        mode,
        citation_enabled: citationEnabled,
        reuse_policy: reusePolicy,
      });
      setValue('');
      setShowForm(false);
      setNotice(`Источник #${created.id} добавлен`);
      await load();
    });
  };

  const doctor = (source: SourceConnectorView) =>
    run(`doctor:${source.id}`, async () => {
      const checked = await studioApi.sourceDoctor(channel!.id, source.id);
      setSources((current) =>
        current.map((row) => (row.id === checked.id ? checked : row)),
      );
      setNotice(
        checked.status === 'healthy'
          ? `Источник #${source.id}: соединение работает`
          : `Источник #${source.id}: ${checked.status}`,
      );
    });

  const ingest = (source: SourceConnectorView) =>
    run(`ingest:${source.id}`, async () => {
      const result = await studioApi.ingestSource(channel!.id, source.id);
      setNotice(
        `Источник #${source.id}: ${result.documents_created} новых документов, ${result.candidates_created} кандидатов отправлено в Inbox`,
      );
      await load();
    });

  const saveSettings = (source: SourceConnectorView, patch: SourceSettingsPatch) =>
    run(`settings:${source.id}`, async () => {
      const updated = await updateSourceSettings(channel!.id, source.id, patch);
      setSources((current) =>
        current.map((row) => (row.id === updated.id ? updated : row)),
      );
      setNotice(
        updated.enabled
          ? `Источник #${updated.id}: настройки сохранены`
          : `Источник #${updated.id}: выключен`,
      );
    });

  if (!channel) {
    return <div className="sources-empty-page">Выберите канал, чтобы настроить источники.</div>;
  }

  return (
    <div className="sources-page">
      <header className="sources-topbar">
        <div>
          <small className="eyebrow">{channel.title || channel.tg_chat_id}</small>
          <h1>Источники</h1>
          <p>Connectors и ingestion pipeline. Нормализованные материалы после ingest появляются в отдельном Inbox.</p>
        </div>
        <div className="top-actions">
          <button className="button secondary" onClick={() => void load()} disabled={busyId !== null}>↻ Обновить</button>
          <button className="button primary" onClick={() => setShowForm((current) => !current)}>+ Источник</button>
        </div>
      </header>

      {error && <div className="banner error" role="alert">{error}<button onClick={() => setError(null)}>×</button></div>}
      {notice && <div className="banner success">{notice}<button onClick={() => setNotice(null)}>×</button></div>}

      {showForm && (
        <form className="source-create-card" onSubmit={(event) => void createSource(event)}>
          <div className="source-form-grid">
            <label>
              <span>Тип</span>
              <select value={kind} onChange={(event) => setKind(event.target.value as CreateSourceInput['kind'])}>
                <option value="rss">RSS / Atom</option>
                <option value="url">Web URL</option>
                <option value="telegram">Telegram</option>
              </select>
            </label>
            <label className="source-value-field">
              <span>{kind === 'telegram' ? 'Username / chat' : 'URL'}</span>
              <input
                required
                value={value}
                onChange={(event) => setValue(event.target.value)}
                placeholder={kind === 'telegram' ? '@channel или id' : 'https://example.com/feed.xml'}
              />
            </label>
            <label>
              <span>Режим</span>
              <select value={mode} onChange={(event) => setMode(event.target.value as CreateSourceInput['mode'])}>
                <option value="summary">Summary</option>
                <option value="rewrite">Rewrite</option>
              </select>
            </label>
            <label>
              <span>Reuse policy</span>
              <select
                value={reusePolicy}
                onChange={(event) => setReusePolicy(event.target.value as CreateSourceInput['reuse_policy'])}
              >
                <option value="reference_only">Только reference</option>
                <option value="summarize">Можно summarization</option>
                <option value="quote_with_attribution">Цитаты с attribution</option>
                <option value="rewrite_with_attribution">Rewrite с attribution</option>
                <option value="mirror_authorized">Mirror разрешён</option>
              </select>
            </label>
          </div>
          <label className="source-checkbox">
            <input type="checkbox" checked={citationEnabled} onChange={(event) => setCitationEnabled(event.target.checked)} />
            <span>Сохранять attribution / citation metadata</span>
          </label>
          <div className="source-form-actions">
            <button type="button" className="button secondary" onClick={() => setShowForm(false)}>Отмена</button>
            <button type="submit" className="button primary" disabled={busyId !== null || !value.trim()}>Добавить</button>
          </div>
        </form>
      )}

      <section className="sources-connectors-card sources-connectors-card-full">
        <div className="panel-heading">
          <div><h2>Connectors</h2><small>{sources.length} подключено</small></div>
        </div>
        <div className="source-list">
          {sources.length === 0 && <div className="empty-state">Источников пока нет.</div>}
          {sources.map((source) => {
            const canIngest = source.enabled && ['rss', 'url', 'web', 'telegram'].includes(source.kind);
            const ingestLabel = source.kind === 'telegram' ? 'MTProto ingest' : 'Ingest now';
            const lifecycleEditable = !(
              source.legacy_grab_source_id !== null && source.legacy_ai_source_id === null
            );
            return (
              <article
                key={source.id}
                className={source.enabled ? 'source-card' : 'source-card source-card-disabled'}
              >
                <div className="source-card-head">
                  <div className={`source-kind source-kind-${source.kind}`}>{sourceKindLabel(source.kind)}</div>
                  <span className={`source-health source-health-${source.status}`}>{source.status}</span>
                </div>
                <strong className="source-address">{source.value}</strong>
                <div className="source-meta-row">
                  <span>{source.mode}</span>
                  <span>{source.reuse_policy}</span>
                  <span>{source.citation_enabled ? 'citation on' : 'citation off'}</span>
                  {source.kind === 'telegram' && source.cursor_message_id !== null && (
                    <span>cursor #{source.cursor_message_id}</span>
                  )}
                  {source.kind === 'telegram' && source.backlog_hint && (
                    <span>backlog: догоняет</span>
                  )}
                  {source.worker_failure_count > 0 && (
                    <span>
                      worker backoff ×{source.worker_failure_count} до {dateLabel(source.worker_retry_after)}
                    </span>
                  )}
                </div>
                {source.status_reason && <p className="source-reason">{source.status_reason}</p>}
                <div className="source-times">
                  <span>success: {dateLabel(source.last_success_at)}</span>
                  <span>document: {dateLabel(source.last_document_at)}</span>
                </div>
                {lifecycleEditable ? (
                  <SourceSettingsControls
                    source={source}
                    busy={busyId === `settings:${source.id}`}
                    onSave={(patch) => saveSettings(source, patch)}
                  />
                ) : (
                  <div className="source-readonly-note">
                    Legacy GrabSource · read-only в Sources v2. Управляйте правилом через legacy grabber.
                  </div>
                )}
                <div className="source-actions">
                  <button
                    className="button secondary compact"
                    disabled={busyId !== null}
                    onClick={() => void doctor(source)}
                  >
                    {busyId === `doctor:${source.id}` ? 'Проверяю…' : 'Doctor'}
                  </button>
                  <button
                    className="button secondary compact"
                    disabled={busyId !== null || !canIngest}
                    title={
                      !source.enabled
                        ? 'Сначала включите источник'
                        : source.worker_failure_count > 0
                          ? 'Ручной ingest проверит источник сейчас и сбросит backoff при успехе'
                          : source.kind === 'telegram'
                            ? 'Получить историю через userbot MTProto session'
                            : 'Получить новые документы сейчас'
                    }
                    onClick={() => void ingest(source)}
                  >
                    {busyId === `ingest:${source.id}` ? 'Читаю…' : ingestLabel}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      </section>
    </div>
  );
}
