import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { StudioApiError, studioApi, type CreateSourceInput } from './api';
import type { Channel, ContentCandidateView, SourceConnectorView } from './types';

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

function actionLabel(action: string | null): string {
  switch (action) {
    case 'summarize': return 'Суммаризировать';
    case 'rewrite': return 'Переписать';
    case 'mirror': return 'Разрешённый mirror';
    case 'review': return 'Проверить права';
    case 'research': return 'Исследовать';
    default: return action || 'Проверить';
  }
}

export function SourcesPanel({
  channel,
  onOpenContent,
}: {
  channel: Channel | null;
  onOpenContent: (contentId: number) => void;
}) {
  const [sources, setSources] = useState<SourceConnectorView[]>([]);
  const [candidates, setCandidates] = useState<ContentCandidateView[]>([]);
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
      setCandidates([]);
      return;
    }
    const [nextSources, nextCandidates] = await Promise.all([
      studioApi.sources(channel.id),
      studioApi.candidates(channel.id),
    ]);
    setSources(nextSources);
    setCandidates(nextCandidates);
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
      setNotice(
        checked.status === 'healthy'
          ? `Источник #${source.id}: соединение работает`
          : `Источник #${source.id}: ${checked.status}`,
      );
      await load();
    });

  const ingest = (source: SourceConnectorView) =>
    run(`ingest:${source.id}`, async () => {
      const result = await studioApi.ingestSource(channel!.id, source.id);
      setNotice(
        `Источник #${source.id}: ${result.documents_created} новых документов, ${result.candidates_created} кандидатов`,
      );
      await load();
    });

  const dismiss = (candidate: ContentCandidateView) =>
    run(`candidate:${candidate.id}`, async () => {
      await studioApi.dismissCandidate(channel!.id, candidate.id);
      setCandidates((current) => current.filter((row) => row.id !== candidate.id));
    });

  const acceptDraft = (candidate: ContentCandidateView) =>
    run(`draft:${candidate.id}`, async () => {
      const draft = await studioApi.candidateDraft(channel!.id, candidate.id);
      setCandidates((current) => current.filter((row) => row.id !== candidate.id));
      setNotice(`Создан черновик #${draft.id} с policy ${candidate.reuse_policy}`);
      onOpenContent(draft.id);
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
          <p>Research pipeline: connector → normalized document → candidate. Автокопирование не включается само.</p>
        </div>
        <div className="top-actions">
          <button className="button secondary" onClick={() => void load()} disabled={busyId !== null}>↻ Обновить</button>
          <button className="button primary" onClick={() => setShowForm((value) => !value)}>+ Источник</button>
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

      <div className="sources-layout">
        <section className="sources-connectors-card">
          <div className="panel-heading">
            <div><h2>Connectors</h2><small>{sources.length} подключено</small></div>
          </div>
          <div className="source-list">
            {sources.length === 0 && <div className="empty-state">Источников пока нет.</div>}
            {sources.map((source) => {
              const canIngest = ['rss', 'url', 'web', 'telegram'].includes(source.kind);
              const ingestLabel = source.kind === 'telegram' ? 'MTProto ingest' : 'Ingest now';
              return (
                <article key={source.id} className="source-card">
                  <div className="source-card-head">
                    <div className={`source-kind source-kind-${source.kind}`}>{sourceKindLabel(source.kind)}</div>
                    <span className={`source-health source-health-${source.status}`}>{source.status}</span>
                  </div>
                  <strong className="source-address">{source.value}</strong>
                  <div className="source-meta-row">
                    <span>{source.mode}</span>
                    <span>{source.reuse_policy}</span>
                    <span>{source.citation_enabled ? 'citation on' : 'citation off'}</span>
                  </div>
                  {source.status_reason && <p className="source-reason">{source.status_reason}</p>}
                  <div className="source-times">
                    <span>success: {dateLabel(source.last_success_at)}</span>
                    <span>document: {dateLabel(source.last_document_at)}</span>
                  </div>
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
                      title={source.kind === 'telegram' ? 'Получить историю через userbot MTProto session' : 'Получить новые документы сейчас'}
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

        <section className="sources-inbox-card">
          <div className="panel-heading">
            <div><h2>Inbox</h2><small>{candidates.length} новых кандидатов</small></div>
          </div>
          <div className="candidate-list">
            {candidates.length === 0 && (
              <div className="empty-state">Новых материалов нет. Worker и ручной ingest будут добавлять их сюда.</div>
            )}
            {candidates.map((candidate) => (
              <article key={candidate.id} className="candidate-card">
                <div className="candidate-head">
                  <span className="candidate-action">{actionLabel(candidate.suggested_action)}</span>
                  <span className="candidate-policy">{candidate.reuse_policy}</span>
                </div>
                <h3>{candidate.source_title || `Материал #${candidate.source_document_id}`}</h3>
                <p>{candidate.summary || candidate.excerpt}</p>
                <div className="candidate-footer">
                  <span>{dateLabel(candidate.published_at || candidate.fetched_at)}</span>
                  <div>
                    {candidate.source_url && (
                      <a className="button secondary compact" href={candidate.source_url} target="_blank" rel="noreferrer">Источник ↗</a>
                    )}
                    <button
                      className="button primary compact"
                      disabled={busyId !== null}
                      onClick={() => void acceptDraft(candidate)}
                    >
                      {busyId === `draft:${candidate.id}` ? 'Создаю…' : 'В черновик'}
                    </button>
                    <button
                      className="button secondary compact"
                      disabled={busyId !== null}
                      onClick={() => void dismiss(candidate)}
                    >
                      Скрыть
                    </button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
