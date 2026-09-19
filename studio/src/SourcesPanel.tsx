import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';

import { StudioApiError, studioApi, type CreateSourceInput } from './api';
import { AsyncRegion, InlineStatus, SkeletonBlock } from './AsyncUI';
import {
  ChannelRequestOwnership,
  resolveChannelDataView,
  type ChannelLoadState,
} from './asyncControl';
import {
  buildSourceCreateInput,
  CHANNEL_DM_SOURCE_KIND,
  hasChannelDMSource,
  sourceCreateNeedsValue,
} from './channelDMSourceCreate';
import {
  parseSourceCreateKindPreference,
  serializeSourceCreateKindPreference,
} from './sourceCreateKindPreference';
import type { SourceCreateKindPreference } from './sourceCreateKindPreference';
import { SourceSettingsControls } from './SourceSettingsControls';
import { SourceWorkerHealthCard } from './SourceWorkerHealthCard';
import { updateSourceSettings } from './sourceSettingsApi';
import type { SourceSettingsPatch } from './sourceSettingsApi';
import {
  getTelegramDeviceStorageItem,
  setTelegramDeviceStorageItem,
} from './telegram';
import {
  SOURCE_CREATE_CANCEL_LABEL,
  useTelegramSourceCreateSecondaryButton,
} from './telegramSourceCreateSecondaryButton';
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
  if (kind === CHANNEL_DM_SOURCE_KIND) return 'Channel DMs';
  return kind;
}

export function SourcesPanel({ channel }: { channel: Channel | null }) {
  const [sources, setSources] = useState<SourceConnectorView[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [loadState, setLoadState] = useState<ChannelLoadState | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<{ channelId: number; message: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [kind, setKind] = useState<SourceCreateKindPreference>('rss');
  const [value, setValue] = useState('');
  const [mode, setMode] = useState<CreateSourceInput['mode']>('summary');
  const [reusePolicy, setReusePolicy] = useState<CreateSourceInput['reuse_policy']>('reference_only');
  const [citationEnabled, setCitationEnabled] = useState(true);
  const kindTouchedRef = useRef(false);
  const requestOwnershipRef = useRef(new ChannelRequestOwnership());
  const validDataChannelRef = useRef<number | null>(null);
  const channelIdRef = useRef<number | null>(channel?.id ?? null);
  channelIdRef.current = channel?.id ?? null;

  const closeCreateSourceForm = useCallback(() => {
    setShowForm(false);
  }, []);

  useTelegramSourceCreateSecondaryButton(showForm, closeCreateSourceForm);

  const load = useCallback(async (): Promise<boolean> => {
    const channelId = channelIdRef.current;
    if (channelId === null) {
      requestOwnershipRef.current.invalidate();
      validDataChannelRef.current = null;
      setSources([]);
      setLoadState(null);
      setRefreshing(false);
      setError(null);
      return false;
    }

    const token = requestOwnershipRef.current.begin(channelId);
    const hasValidData = validDataChannelRef.current === channelId;
    const isCurrent = () => requestOwnershipRef.current.isCurrent(token, channelIdRef.current);

    setRefreshing(true);
    setError(null);
    if (!hasValidData) {
      validDataChannelRef.current = null;
      setSources([]);
      setLoadState({ channelId, phase: 'loading' });
    }

    try {
      const rows = await studioApi.sources(channelId);
      if (!isCurrent()) return false;
      validDataChannelRef.current = channelId;
      setSources(rows);
      setLoadState({ channelId, phase: 'loaded' });
      return true;
    } catch (reason) {
      if (!isCurrent()) return false;
      if (validDataChannelRef.current !== channelId) {
        setSources([]);
        setLoadState({ channelId, phase: 'error-without-valid-data' });
      }
      setError({ channelId, message: errorMessage(reason) });
      return false;
    } finally {
      if (isCurrent()) setRefreshing(false);
    }
  }, [channel?.id]);

  useEffect(() => {
    setNotice(null);
    void load();
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    void getTelegramDeviceStorageItem('editor-ui-preferences').then((result) => {
      if (cancelled || kindTouchedRef.current || !result.ok) return;
      const storedKind = parseSourceCreateKindPreference(result.value);
      if (storedKind) setKind(storedKind);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const persistSourceKindPreference = useCallback(
    async (nextKind: SourceCreateKindPreference) => {
      const current = await getTelegramDeviceStorageItem('editor-ui-preferences');
      const currentValue = current.ok ? current.value : null;
      await setTelegramDeviceStorageItem(
        'editor-ui-preferences',
        serializeSourceCreateKindPreference(currentValue, nextKind),
      );
    },
    [],
  );

  const run = async (key: string, action: () => Promise<void>) => {
    const operationChannelId = channelIdRef.current;
    setBusyId(key);
    setError(null);
    try {
      await action();
    } catch (reason) {
      if (operationChannelId !== null && channelIdRef.current === operationChannelId) {
        setError({ channelId: operationChannelId, message: errorMessage(reason) });
      }
    } finally {
      setBusyId(null);
    }
  };

  const channelDMSourceExists = hasChannelDMSource(sources.map((source) => source.kind));
  const sourceValueReady = !sourceCreateNeedsValue(kind) || Boolean(value.trim());
  const duplicateChannelDMSource = kind === CHANNEL_DM_SOURCE_KIND && channelDMSourceExists;
  const sourceDataView = channel
    ? resolveChannelDataView(loadState, channel.id, sources.length)
    : null;
  const initialLoading = sourceDataView === 'loading';
  const loadFailedWithoutValidData = sourceDataView === 'error-without-valid-data';
  const hasValidData = sourceDataView === 'loaded-data' || sourceDataView === 'loaded-empty';

  const createSource = async (event: FormEvent) => {
    event.preventDefault();
    if (!channel || duplicateChannelDMSource) return;
    const input = buildSourceCreateInput({
      kind,
      value,
      channelTgChatId: channel.tg_chat_id,
      mode,
      citationEnabled,
      reusePolicy,
    });
    if (!input) return;

    await run('create', async () => {
      const created = await studioApi.createSource(channel.id, input);
      kindTouchedRef.current = true;
      void persistSourceKindPreference(kind);
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
          <button
            className="button secondary"
            onClick={() => void load()}
            disabled={refreshing || busyId !== null}
          >
            ↻ Обновить
          </button>
          <button className="button primary" onClick={() => setShowForm((current) => !current)}>+ Источник</button>
          {refreshing && hasValidData && <InlineStatus>Обновляю источники…</InlineStatus>}
        </div>
      </header>

      {error?.channelId === channel.id && (
        <div className="banner error" role="alert">
          {error.message}
          <button aria-label="Закрыть ошибку" onClick={() => setError(null)}>×</button>
        </div>
      )}
      {notice && <div className="banner success">{notice}<button onClick={() => setNotice(null)}>×</button></div>}

      <SourceWorkerHealthCard />

      {showForm && (
        <form className="source-create-card" onSubmit={(event) => void createSource(event)}>
          <div className="source-form-grid">
            <label>
              <span>Тип</span>
              <select
                value={kind}
                onChange={(event) => {
                  kindTouchedRef.current = true;
                  setKind(event.target.value as SourceCreateKindPreference);
                }}
              >
                <option value="rss">RSS / Atom</option>
                <option value="url">Web URL</option>
                <option value="telegram">Telegram</option>
                <option value={CHANNEL_DM_SOURCE_KIND} disabled={channelDMSourceExists}>
                  Channel DMs{channelDMSourceExists ? ' · уже подключено' : ''}
                </option>
              </select>
            </label>
            {kind === CHANNEL_DM_SOURCE_KIND ? (
              <div className="source-value-field">
                <span>Канал</span>
                <strong>{channel.title || 'Текущий Telegram-канал'}</strong>
                <small>Входящие Channel DMs привязываются к текущему owned channel; отдельный routing ID не вводится.</small>
              </div>
            ) : (
              <label className="source-value-field">
                <span>{kind === 'telegram' ? 'Username / chat' : 'URL'}</span>
                <input
                  required
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  placeholder={kind === 'telegram' ? '@channel или id' : 'https://example.com/feed.xml'}
                />
              </label>
            )}
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
            <button type="button" className="button secondary" onClick={closeCreateSourceForm}>{SOURCE_CREATE_CANCEL_LABEL}</button>
            <button
              type="submit"
              className="button primary"
              disabled={busyId !== null || !sourceValueReady || duplicateChannelDMSource}
            >
              Добавить
            </button>
          </div>
        </form>
      )}

      <section className="sources-connectors-card sources-connectors-card-full">
        <div className="panel-heading">
          <div>
            <h2>Connectors</h2>
            <small>{initialLoading ? 'Загружаю…' : `${sources.length} подключено`}</small>
          </div>
        </div>
        <AsyncRegion
          className="source-list"
          loading={initialLoading}
          error={loadFailedWithoutValidData}
          empty={sourceDataView === 'loaded-empty'}
          loadingLabel="Загружаю источники…"
          loadingFallback={
            <>
              {[0, 1, 2].map((index) => (
                <article className="source-card ai-card-skeleton" key={index}>
                  <SkeletonBlock height={18} width="28%" radius={999} />
                  <SkeletonBlock height={14} width={index % 2 ? '72%' : '84%'} />
                  <SkeletonBlock height={10} width="58%" />
                </article>
              ))}
            </>
          }
          emptyFallback={<div className="empty-state">Источников пока нет.</div>}
          errorFallback={<div className="empty-state">Источники не загружены. Повторите попытку.</div>}
        >
          {sources.map((source) => {
            const canIngest =
              source.enabled &&
              !source.ingestion_busy &&
              ['rss', 'url', 'web', 'telegram'].includes(source.kind);
            const ingestLabel =
              source.kind === CHANNEL_DM_SOURCE_KIND
                ? 'Push ingress'
                : source.kind === 'telegram'
                  ? 'MTProto ingest'
                  : 'Ingest now';
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
                <strong className="source-address">
                  {source.kind === CHANNEL_DM_SOURCE_KIND ? 'Входящие DM текущего Telegram-канала' : source.value}
                </strong>
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
                  {source.ingestion_busy && (
                    <span>
                      ingest: {source.ingestion_holder || 'busy'} до {dateLabel(source.ingestion_lease_expires_at)}
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
                    busy={busyId === `settings:${source.id}` || source.ingestion_busy}
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
                        : source.kind === CHANNEL_DM_SOURCE_KIND
                          ? 'Channel DMs поступают через bot updates; ручной ingest не требуется'
                          : source.ingestion_busy
                            ? `Ingest уже выполняет ${source.ingestion_holder || 'другой процесс'}`
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
        </AsyncRegion>
      </section>
    </div>
  );
}
