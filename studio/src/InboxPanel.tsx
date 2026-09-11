import { useCallback, useEffect, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import {
  applyCurrentStructuredRewrite,
  loadCurrentStructuredRewritePreviews,
  type RewritePreview,
} from './candidateRewriteAuthority';
import {
  candidateMediaLabel,
  loadCandidateMedia,
  promoteCandidateMedia,
  type CandidateMediaView,
} from './candidateMedia';
import { TelegramVisualPreview } from './TelegramVisualPreview';
import type { Channel, ContentCandidateView } from './types';

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

function scoreLabel(score: number | null): string | null {
  if (score === null || !Number.isFinite(score)) return null;
  return `${Math.round(Math.max(0, Math.min(1, score)) * 100)}%`;
}

function mediaMap(rows: CandidateMediaView[]): Record<number, CandidateMediaView> {
  return Object.fromEntries(rows.map((row) => [row.candidate_id, row]));
}

export function InboxPanel({
  channel,
  onOpenContent,
}: {
  channel: Channel | null;
  onOpenContent: (contentId: number) => void;
}) {
  const [candidates, setCandidates] = useState<ContentCandidateView[]>([]);
  const [candidateMedia, setCandidateMedia] = useState<Record<number, CandidateMediaView>>({});
  const [rewritePreviews, setRewritePreviews] = useState<Record<number, RewritePreview>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!channel) {
      setCandidates([]);
      setCandidateMedia({});
      setRewritePreviews({});
      return;
    }
    const [rows, mediaRows] = await Promise.all([
      studioApi.candidates(channel.id),
      loadCandidateMedia(channel.id),
    ]);
    setCandidates(rows);
    setCandidateMedia(mediaMap(mediaRows));
    setRewritePreviews(await loadCurrentStructuredRewritePreviews(channel.id, rows));
  }, [channel]);

  useEffect(() => {
    setError(null);
    setNotice(null);
    setRewritePreviews({});
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

  const enrichBatch = () =>
    run('batch-local', async () => {
      const result = await studioApi.enrichCandidatesLocalBatch(channel!.id, 50);
      await load();
      setNotice(
        `Local batch: выбрано ${result.selected}, новых ${result.completed}, reused ${result.reused}, failed ${result.failed}`,
      );
    });

  const enrichLocal = (candidate: ContentCandidateView) =>
    run(`enrich-local:${candidate.id}`, async () => {
      const result = await studioApi.enrichCandidateLocal(channel!.id, candidate.id);
      setCandidates((current) =>
        current.map((row) =>
          row.id === candidate.id
            ? { ...row, summary: result.summary, topic: result.topic, score: result.score }
            : row,
        ),
      );
      setNotice(
        result.reused_existing
          ? `Local enrichment #${result.run_id}: использован сохранённый результат`
          : `Local enrichment #${result.run_id}: готов`,
      );
    });

  const enrichAI = (candidate: ContentCandidateView) =>
    run(`enrich-ai:${candidate.id}`, async () => {
      const result = await studioApi.enrichCandidateAI(channel!.id, candidate.id);
      setCandidates((current) =>
        current.map((row) =>
          row.id === candidate.id
            ? { ...row, summary: result.summary, topic: result.topic, score: result.score }
            : row,
        ),
      );
      setNotice(
        result.reused_existing
          ? `AI enrichment #${result.run_id}: использован сохранённый результат`
          : `AI enrichment #${result.run_id}: ${result.model || result.provider}`,
      );
    });

  const rewriteAI = (candidate: ContentCandidateView) =>
    run(`rewrite-ai:${candidate.id}`, async () => {
      const result = await studioApi.rewriteCandidateAI(channel!.id, candidate.id);
      setRewritePreviews((current) => ({
        ...current,
        [candidate.id]: {
          runId: result.run_id,
          text: result.text,
          model: result.model,
          kind: 'text',
          document: null,
        },
      }));
      setNotice(
        result.reused_existing
          ? `AI rewrite #${result.run_id}: использован сохранённый результат`
          : `AI rewrite #${result.run_id}: ${result.model || result.provider}`,
      );
    });

  const rewriteStructuredAI = (candidate: ContentCandidateView) =>
    run(`rewrite-ai-structured:${candidate.id}`, async () => {
      const result = await studioApi.rewriteCandidateAIStructured(channel!.id, candidate.id);
      setRewritePreviews((current) => ({
        ...current,
        [candidate.id]: {
          runId: result.run_id,
          text: result.text,
          model: result.model,
          kind: 'structured',
          document: result.document,
        },
      }));
      setNotice(
        result.reused_existing
          ? `AI Rich #${result.run_id}: использован сохранённый PostDocument`
          : `AI Rich #${result.run_id}: validated PostDocument · ${result.model || result.provider}`,
      );
    });

  const promoteMedia = (candidate: ContentCandidateView) =>
    run(`promote-media:${candidate.id}`, async () => {
      const asset = await promoteCandidateMedia(channel!.id, candidate.id);
      setCandidateMedia((current) => {
        const media = current[candidate.id];
        if (!media) return current;
        return {
          ...current,
          [candidate.id]: { ...media, media_asset_id: asset.id },
        };
      });
      setNotice(`MediaAsset #${asset.id} сохранён в медиатеке канала`);
    });

  const dismiss = (candidate: ContentCandidateView) =>
    run(`dismiss:${candidate.id}`, async () => {
      await studioApi.dismissCandidate(channel!.id, candidate.id);
      setCandidates((current) => current.filter((row) => row.id !== candidate.id));
      setCandidateMedia((current) => {
        const next = { ...current };
        delete next[candidate.id];
        return next;
      });
      setRewritePreviews((current) => {
        const next = { ...current };
        delete next[candidate.id];
        return next;
      });
    });

  const acceptDraft = (candidate: ContentCandidateView) =>
    run(`draft:${candidate.id}`, async () => {
      const preview = rewritePreviews[candidate.id];
      const draft = preview?.kind === 'structured'
        ? await applyCurrentStructuredRewrite(channel!.id, candidate.id, preview.runId)
        : await studioApi.candidateDraft(channel!.id, candidate.id);
      setCandidates((current) => current.filter((row) => row.id !== candidate.id));
      setCandidateMedia((current) => {
        const next = { ...current };
        delete next[candidate.id];
        return next;
      });
      setRewritePreviews((current) => {
        const next = { ...current };
        delete next[candidate.id];
        return next;
      });
      onOpenContent(draft.id);
    });

  if (!channel) {
    return <div className="sources-empty-page">Выберите канал, чтобы открыть Inbox.</div>;
  }

  return (
    <div className="sources-page inbox-page">
      <header className="sources-topbar">
        <div>
          <small className="eyebrow">{channel.title || channel.tg_chat_id}</small>
          <h1>Inbox</h1>
          <p>Нормализованные кандидаты из Telegram/RSS/Web: анализ → enrichment → policy-safe draft.</p>
        </div>
        <div className="top-actions">
          <button className="button secondary" onClick={() => void load()} disabled={busyId !== null}>↻ Обновить</button>
          <button
            className="button secondary"
            onClick={() => void enrichBatch()}
            disabled={busyId !== null || candidates.length === 0}
            title="Deterministic local enrichment, без AI-токенов"
          >
            {busyId === 'batch-local' ? 'Анализ…' : 'Local batch'}
          </button>
        </div>
      </header>

      {error && <div className="banner error" role="alert">{error}<button onClick={() => setError(null)}>×</button></div>}
      {notice && <div className="banner success">{notice}<button onClick={() => setNotice(null)}>×</button></div>}

      <section className="sources-inbox-card inbox-standalone-card">
        <div className="panel-heading">
          <div><h2>Новые кандидаты</h2><small>{candidates.length} в очереди редактора</small></div>
        </div>
        <div className="candidate-list">
          {candidates.length === 0 && (
            <div className="empty-state">Inbox пуст. Источники и ingestion worker добавят новые материалы сюда.</div>
          )}
          {candidates.map((candidate) => {
            const score = scoreLabel(candidate.score);
            const rewritePreview = rewritePreviews[candidate.id];
            const media = candidateMedia[candidate.id];
            const canRewrite = candidate.reuse_policy === 'rewrite_with_attribution';
            const canPromoteMedia = Boolean(media?.promotable && !media.media_asset_id);
            return (
              <article key={candidate.id} className="candidate-card">
                <div className="candidate-head">
                  <span className="candidate-action">{actionLabel(candidate.suggested_action)}</span>
                  <span className="candidate-policy">{candidate.reuse_policy}</span>
                  {media && <span className="candidate-policy">{candidateMediaLabel(media)}</span>}
                  {score && <span className="candidate-policy">score {score}</span>}
                </div>
                <h3>{candidate.topic || candidate.source_title || `Материал #${candidate.source_document_id}`}</h3>
                <p>{candidate.summary || candidate.excerpt}</p>
                {rewritePreview && (
                  <div className="candidate-rewrite-preview">
                    <small>
                      {rewritePreview.kind === 'structured' ? 'AI Rich preview' : 'AI rewrite preview'} · run #{rewritePreview.runId}
                      {rewritePreview.model ? ` · ${rewritePreview.model}` : ''}
                    </small>
                    {rewritePreview.document ? (
                      <details>
                        <summary>Visual PostDocument preview</summary>
                        <TelegramVisualPreview document={rewritePreview.document} channel={channel} />
                      </details>
                    ) : (
                      <p>{rewritePreview.text}</p>
                    )}
                  </div>
                )}
                <div className="candidate-footer">
                  <span>{dateLabel(candidate.published_at || candidate.fetched_at)}</span>
                  <div>
                    {candidate.source_url && (
                      <a className="button secondary compact" href={candidate.source_url} target="_blank" rel="noreferrer">Источник ↗</a>
                    )}
                    {canPromoteMedia && (
                      <button
                        className="button secondary compact"
                        disabled={busyId !== null}
                        title="Сохранить исходное Telegram media как reusable MediaAsset текущего канала"
                        onClick={() => void promoteMedia(candidate)}
                      >
                        {busyId === `promote-media:${candidate.id}` ? 'Сохраняю media…' : '▣ В медиатеку'}
                      </button>
                    )}
                    <button
                      className="button secondary compact"
                      disabled={busyId !== null}
                      onClick={() => void enrichLocal(candidate)}
                    >
                      {busyId === `enrich-local:${candidate.id}` ? 'Анализ…' : 'Local'}
                    </button>
                    <button
                      className="button secondary compact"
                      disabled={busyId !== null}
                      title="Использует AI-настройки и лимиты выбранного канала"
                      onClick={() => void enrichAI(candidate)}
                    >
                      {busyId === `enrich-ai:${candidate.id}` ? 'AI…' : '✨ AI'}
                    </button>
                    {canRewrite && (
                      <button
                        className="button secondary compact"
                        disabled={busyId !== null}
                        title="Создать независимый AI rewrite; attribution добавит приложение"
                        onClick={() => void rewriteAI(candidate)}
                      >
                        {busyId === `rewrite-ai:${candidate.id}` ? 'Rewrite…' : '✨ Rewrite'}
                      </button>
                    )}
                    {canRewrite && (
                      <button
                        className="button secondary compact"
                        disabled={busyId !== null}
                        title="Сгенерировать validated Rich PostDocument без автоматического применения"
                        onClick={() => void rewriteStructuredAI(candidate)}
                      >
                        {busyId === `rewrite-ai-structured:${candidate.id}` ? 'Rich…' : '✨ Rich'}
                      </button>
                    )}
                    <button
                      className="button primary compact"
                      disabled={busyId !== null}
                      onClick={() => void acceptDraft(candidate)}
                    >
                      {busyId === `draft:${candidate.id}`
                        ? 'Создаю…'
                        : rewritePreview?.kind === 'structured'
                          ? 'В Rich черновик'
                          : 'В черновик'}
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
            );
          })}
        </div>
      </section>
    </div>
  );
}
