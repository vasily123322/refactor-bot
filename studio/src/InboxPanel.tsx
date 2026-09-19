import { useCallback, useEffect, useRef, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import { AsyncRegion, InlineStatus, SkeletonBlock } from './AsyncUI';
import {
  ChannelRequestOwnership,
  ScopedExclusiveOperationLock,
  resolveScopedDataView,
  runScopedExclusiveOperation,
  type ChannelRequestToken,
  type ScopedLoadState,
} from './asyncControl';
import { ChannelDMProvenance } from './ChannelDMProvenance';
import {
  INBOX_HISTORY_PAGE_SIZE,
  inboxHistoryCursor,
  mergeInboxHistoryPage,
  splitInboxHistoryPage,
} from './inboxCandidateHistory';
import {
  INBOX_STATUS_OPTIONS,
  inboxStatusPresentation,
  inboxStatusScope,
  type InboxCandidateStatus,
} from './inboxCandidateStatus';
import { channelDMEnrichmentSummary } from './channelDMPresentation';
import { promptCandidateStructuredRewrite } from './candidatePromptRewrite';
import {
  applyCurrentStructuredRewrite,
  isRewriteAuthorityStaleError,
  loadCurrentStructuredRewritePreviews,
  rewriteProvenanceLabel,
  type RewritePreview,
} from './candidateRewriteAuthority';
import {
  editCurrentStructuredRewrite,
  type StructuredEditOperation,
} from './candidateStructuredEdit';
import {
  candidateMediaLabel,
  candidateMediaMap,
  loadCandidateMediaForCandidates,
  mergeCandidateMediaMap,
  promoteCandidateMedia,
  type CandidateMediaView,
} from './candidateMedia';
import { SuggestedPostProvenance } from './SuggestedPostProvenance';
import {
  candidateInboxPrimaryText,
  suggestedPostEnrichmentSummary,
} from './suggestedPostPresentation';
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

const structuredEditLabels: Array<[StructuredEditOperation, string]> = [
  ['shorten', 'Короче'],
  ['expand', 'Подробнее'],
  ['to_list', 'Списком'],
  ['add_headings', 'Заголовки'],
];

export function InboxPanel({
  channel,
  onOpenContent,
}: {
  channel: Channel | null;
  onOpenContent: (contentId: number) => void;
}) {
  const [statusView, setStatusView] = useState<InboxCandidateStatus>('new');
  const [candidates, setCandidates] = useState<ContentCandidateView[]>([]);
  const [candidateMedia, setCandidateMedia] = useState<Record<number, CandidateMediaView>>({});
  const [rewritePreviews, setRewritePreviews] = useState<Record<number, RewritePreview>>({});
  const [rewriteInstructions, setRewriteInstructions] = useState<Record<number, string>>({});
  const [busyKeys, setBusyKeys] = useState<Set<string>>(() => new Set());
  const [loadState, setLoadState] = useState<ScopedLoadState | null>(null);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const [historyLoadMoreError, setHistoryLoadMoreError] = useState<string | null>(null);
  const [error, setError] = useState<{
    channelId: number;
    scopeKey: string;
    message: string;
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const requestOwnershipRef = useRef(new ChannelRequestOwnership());
  const operationContextOwnershipRef = useRef(new ChannelRequestOwnership());
  const operationContextRef = useRef<{
    channelId: number | null;
    scopeKey: string | null;
    token: ChannelRequestToken | null;
  }>({ channelId: null, scopeKey: null, token: null });
  const operationLocksRef = useRef(new Map<number, ScopedExclusiveOperationLock>());
  const loadMoreInFlightRef = useRef(new Set<string>());
  const validDataScopeRef = useRef<string | null>(null);
  const channelIdRef = useRef<number | null>(channel?.id ?? null);
  const statusRef = useRef<InboxCandidateStatus>(statusView);
  const scopeKeyRef = useRef<string | null>(
    channel?.id == null ? null : inboxStatusScope(statusView),
  );
  const currentChannelId = channel?.id ?? null;
  const currentScopeKey = currentChannelId === null ? null : inboxStatusScope(statusView);
  if (
    operationContextRef.current.channelId !== currentChannelId
    || operationContextRef.current.scopeKey !== currentScopeKey
  ) {
    operationContextRef.current.channelId = currentChannelId;
    operationContextRef.current.scopeKey = currentScopeKey;
    if (currentChannelId === null || currentScopeKey === null) {
      operationContextOwnershipRef.current.invalidate();
      operationContextRef.current.token = null;
    } else {
      operationContextRef.current.token = operationContextOwnershipRef.current.begin(
        currentChannelId,
        currentScopeKey,
      );
    }
  }
  channelIdRef.current = currentChannelId;
  statusRef.current = statusView;
  scopeKeyRef.current = currentScopeKey;

  const load = useCallback(async (): Promise<boolean> => {
    const channelId = channelIdRef.current;
    const status = statusRef.current;
    const scopeKey = inboxStatusScope(status);
    if (channelId === null) {
      requestOwnershipRef.current.invalidate();
      validDataScopeRef.current = null;
      setCandidates([]);
      setCandidateMedia({});
      setRewritePreviews({});
      setRewriteInstructions({});
      setHistoryHasMore(false);
      setHistoryLoadingMore(false);
      setHistoryLoadMoreError(null);
      setLoadState(null);
      return false;
    }

    const token = requestOwnershipRef.current.begin(channelId, scopeKey);
    const dataScope = `${channelId}:${scopeKey}`;
    const hasValidData = validDataScopeRef.current === dataScope;
    const isCurrent = () => requestOwnershipRef.current.isCurrent(
      token,
      channelIdRef.current,
      scopeKeyRef.current,
    );

    setError(null);
    setHistoryLoadingMore(false);
    setHistoryLoadMoreError(null);
    if (!hasValidData) {
      validDataScopeRef.current = null;
      setCandidates([]);
      setCandidateMedia({});
      setRewritePreviews({});
      setRewriteInstructions({});
      setHistoryHasMore(false);
      setLoadState({ channelId, scopeKey, phase: 'loading' });
    }

    try {
      const rows = await studioApi.candidates(channelId, status, {
        limit: INBOX_HISTORY_PAGE_SIZE + 1,
      });
      if (!isCurrent()) return false;
      const page = splitInboxHistoryPage(rows);
      const [mediaRows, previews] = status === 'new'
        ? await Promise.all([
            loadCandidateMediaForCandidates(
              channelId,
              page.items.map((candidate) => candidate.id),
            ),
            loadCurrentStructuredRewritePreviews(channelId, page.items),
          ])
        : [[], {} as Record<number, RewritePreview>];
      if (!isCurrent()) return false;

      setCandidates(page.items);
      setCandidateMedia(candidateMediaMap(mediaRows));
      setRewritePreviews(previews);
      setHistoryHasMore(page.hasMore);
      validDataScopeRef.current = dataScope;
      setLoadState({ channelId, scopeKey, phase: 'loaded' });
      return true;
    } catch (reason) {
      if (!isCurrent()) return false;
      if (validDataScopeRef.current !== dataScope) {
        setCandidates([]);
        setCandidateMedia({});
        setRewritePreviews({});
        setHistoryHasMore(false);
        setLoadState({ channelId, scopeKey, phase: 'error-without-valid-data' });
      }
      setError({ channelId, scopeKey, message: errorMessage(reason) });
      return false;
    }
  }, []);

  useEffect(() => {
    setError(null);
    setNotice(null);
    void load();
  }, [channel?.id, statusView, load]);


  const loadOlder = useCallback(async (): Promise<void> => {
    const channelId = channelIdRef.current;
    const status = statusRef.current;
    const scopeKey = scopeKeyRef.current;
    if (
      channelId === null
      || scopeKey === null
      || !historyHasMore
    ) return;

    const dataScope = `${channelId}:${scopeKey}`;
    if (loadMoreInFlightRef.current.has(dataScope)) return;

    const cursor = inboxHistoryCursor(candidates);
    if (!cursor) {
      setHistoryHasMore(false);
      return;
    }

    const token = requestOwnershipRef.current.begin(channelId, scopeKey);
    const isCurrent = () => requestOwnershipRef.current.isCurrent(
      token,
      channelIdRef.current,
      scopeKeyRef.current,
    );

    loadMoreInFlightRef.current.add(dataScope);
    setHistoryLoadingMore(true);
    setHistoryLoadMoreError(null);
    try {
      const rows = await studioApi.candidates(channelId, status, {
        limit: INBOX_HISTORY_PAGE_SIZE + 1,
        beforePublishedAt: cursor.publishedAt,
        beforeId: cursor.id,
      });
      if (!isCurrent()) return;
      const page = splitInboxHistoryPage(rows);
      const [mediaRows, previews] = status === 'new'
        ? await Promise.all([
            loadCandidateMediaForCandidates(
              channelId,
              page.items.map((candidate) => candidate.id),
            ),
            loadCurrentStructuredRewritePreviews(channelId, page.items),
          ])
        : [[], {} as Record<number, RewritePreview>];
      if (!isCurrent()) return;

      setCandidates((current) => mergeInboxHistoryPage(current, page.items));
      if (status === 'new') {
        setCandidateMedia((current) => mergeCandidateMediaMap(current, mediaRows));
        setRewritePreviews((current) => ({ ...current, ...previews }));
      }
      setHistoryHasMore(page.hasMore);
    } catch (reason) {
      if (isCurrent()) setHistoryLoadMoreError(errorMessage(reason));
    } finally {
      loadMoreInFlightRef.current.delete(dataScope);
      if (isCurrent()) setHistoryLoadingMore(false);
    }
  }, [candidates, historyHasMore]);

  const run = async (
    key: string,
    action: (isCurrent: () => boolean) => Promise<unknown>,
  ): Promise<boolean> => {
    const operationChannelId = channelIdRef.current;
    const operationScopeKey = scopeKeyRef.current;
    const operationContextToken = operationContextRef.current.token;
    if (
      operationChannelId === null
      || operationScopeKey === null
      || operationContextToken === null
    ) return false;

    let lock = operationLocksRef.current.get(operationChannelId);
    if (!lock) {
      lock = new ScopedExclusiveOperationLock();
      operationLocksRef.current.set(operationChannelId, lock);
    }
    const candidateId = key.split(':')[1] || null;
    const scope = key === 'refresh' || key === 'batch-local'
      ? null
      : candidateId
        ? `candidate:${candidateId}`
        : null;
    const busyKey = `${operationChannelId}:${operationScopeKey}:${key}`;
    const result = await runScopedExclusiveOperation(
      lock,
      scope,
      busyKey,
      async () => {
        const isCurrent = () => operationContextOwnershipRef.current.isCurrent(
          operationContextToken,
          channelIdRef.current,
          scopeKeyRef.current,
        );
        if (isCurrent()) setError(null);
        try {
          await action(isCurrent);
        } catch (reason) {
          if (isCurrent()) {
            setError({
              channelId: operationChannelId,
              scopeKey: operationScopeKey,
              message: errorMessage(reason),
            });
          }
        }
      },
      (activeKey, active) => setBusyKeys((current) => {
        const next = new Set(current);
        if (active) next.add(activeKey);
        else next.delete(activeKey);
        return next;
      }),
    );
    return result.started;
  };

  const currentBusyPrefix = channelIdRef.current === null || scopeKeyRef.current === null
    ? null
    : `${channelIdRef.current}:${scopeKeyRef.current}:`;
  const currentBusyKeys = currentBusyPrefix === null
    ? []
    : Array.from(busyKeys)
      .filter((key) => key.startsWith(currentBusyPrefix))
      .map((key) => key.slice(currentBusyPrefix.length));
  const isBusy = (key: string) => currentBusyKeys.includes(key);
  const hasCurrentBusy = currentBusyKeys.length > 0;
  const globalBusy = isBusy('refresh') || isBusy('batch-local');
  const candidateBusy = (candidateId: number) => currentBusyKeys.some(
    (key) => key.split(':')[1] === String(candidateId),
  );
  const candidateOperationLabel = (candidateId: number): string | null => {
    const key = currentBusyKeys.find((value) => value.split(':')[1] === String(candidateId));
    if (!key) return null;
    if (key.startsWith('enrich-local:')) return 'Анализирую локально…';
    if (key.startsWith('enrich-ai:')) return 'Запускаю AI enrichment…';
    if (key.startsWith('rewrite-ai-prompt:')) return 'Готовлю Rich proposal по промпту…';
    if (key.startsWith('rewrite-ai-edit:')) return 'Редактирую Rich proposal…';
    if (key.startsWith('rewrite-ai-structured:')) return 'Готовлю Rich rewrite…';
    if (key.startsWith('rewrite-ai:')) return 'Готовлю AI rewrite…';
    if (key.startsWith('promote-media:')) return 'Сохраняю media в медиатеку…';
    if (key.startsWith('draft:')) return 'Создаю черновик…';
    if (key.startsWith('dismiss:')) return 'Скрываю материал…';
    if (key.startsWith('restore:')) return 'Восстанавливаю материал…';
    return 'Выполняю действие…';
  };

  const markStructuredPreviewStale = (candidateId: number, runId: number) => {
    setRewritePreviews((current) => {
      const preview = current[candidateId];
      if (!preview || preview.kind !== 'structured' || preview.runId !== runId) return current;
      return {
        ...current,
        [candidateId]: { ...preview, provenanceStatus: 'stale' },
      };
    });
  };

  const enrichBatch = () =>
    run('batch-local', async (isCurrent) => {
      const result = await studioApi.enrichCandidatesLocalBatch(channel!.id, 50);
      if (!isCurrent()) return;
      await load();
      if (!isCurrent()) return;
      setNotice(
        `Local batch: выбрано ${result.selected}, новых ${result.completed}, reused ${result.reused}, failed ${result.failed}`,
      );
    });

  const enrichLocal = (candidate: ContentCandidateView) =>
    run(`enrich-local:${candidate.id}`, async (isCurrent) => {
      const result = await studioApi.enrichCandidateLocal(channel!.id, candidate.id);
      if (!isCurrent()) return;
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
    run(`enrich-ai:${candidate.id}`, async (isCurrent) => {
      const result = await studioApi.enrichCandidateAI(channel!.id, candidate.id);
      if (!isCurrent()) return;
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
    run(`rewrite-ai:${candidate.id}`, async (isCurrent) => {
      const result = await studioApi.rewriteCandidateAI(channel!.id, candidate.id);
      if (!isCurrent()) return;
      setRewritePreviews((current) => ({
        ...current,
        [candidate.id]: {
          runId: result.run_id,
          text: result.text,
          provider: result.provider,
          model: result.model,
          kind: 'text',
          document: null,
          provenanceStatus: 'unavailable',
        },
      }));
      setNotice(
        result.reused_existing
          ? `AI rewrite #${result.run_id}: использован сохранённый результат`
          : `AI rewrite #${result.run_id}: ${result.model || result.provider}`,
      );
    });

  const rewriteStructuredAI = (candidate: ContentCandidateView) =>
    run(`rewrite-ai-structured:${candidate.id}`, async (isCurrent) => {
      const result = await studioApi.rewriteCandidateAIStructured(channel!.id, candidate.id);
      if (!isCurrent()) return;
      setRewritePreviews((current) => ({
        ...current,
        [candidate.id]: {
          runId: result.run_id,
          text: result.text,
          provider: result.provider,
          model: result.model,
          kind: 'structured',
          document: result.document,
          provenanceStatus: 'current_at_last_server_check',
        },
      }));
      setNotice(
        result.reused_existing
          ? `AI Rich #${result.run_id}: использован сохранённый PostDocument`
          : `AI Rich #${result.run_id}: validated PostDocument · ${result.model || result.provider}`,
      );
    });

  const rewritePromptStructuredAI = (candidate: ContentCandidateView) => {
    const instruction = (rewriteInstructions[candidate.id] || '').trim();
    if (!instruction) return;
    void run(`rewrite-ai-prompt:${candidate.id}`, async (isCurrent) => {
      const result = await promptCandidateStructuredRewrite(
        channel!.id,
        candidate.id,
        instruction,
      );
      if (!isCurrent()) return;
      setRewritePreviews((current) => ({
        ...current,
        [candidate.id]: {
          runId: result.run_id,
          text: result.text,
          provider: result.provider,
          model: result.model,
          kind: 'structured',
          document: result.document,
          provenanceStatus: 'current_at_last_server_check',
        },
      }));
      setNotice(
        result.reused_existing
          ? `AI по промпту #${result.run_id}: использован тот же current proposal`
          : `AI по промпту #${result.run_id}: validated PostDocument · ${result.model || result.provider}`,
      );
    });
  };

  const editStructuredAI = (
    candidate: ContentCandidateView,
    operation: StructuredEditOperation,
  ) => {
    const preview = rewritePreviews[candidate.id];
    if (!preview || preview.kind !== 'structured') return;
    void run(`rewrite-ai-edit:${candidate.id}:${operation}`, async (isCurrent) => {
      try {
        const result = await editCurrentStructuredRewrite(
          channel!.id,
          candidate.id,
          preview.runId,
          operation,
        );
        if (!isCurrent()) return;
        setRewritePreviews((current) => ({
          ...current,
          [candidate.id]: {
            runId: result.run_id,
            text: result.text,
            provider: result.provider,
            model: result.model,
            kind: 'structured',
            document: result.document,
            provenanceStatus: 'current_at_last_server_check',
          },
        }));
        setNotice(
          result.reused_existing
            ? `AI edit #${result.run_id}: использован тот же operation proposal`
            : `AI edit #${result.run_id}: ${operation} · validated PostDocument`,
        );
      } catch (reason) {
        if (isCurrent() && isRewriteAuthorityStaleError(reason)) {
          markStructuredPreviewStale(candidate.id, preview.runId);
        }
        throw reason;
      }
    });
  };

  const promoteMedia = (candidate: ContentCandidateView) =>
    run(`promote-media:${candidate.id}`, async (isCurrent) => {
      const asset = await promoteCandidateMedia(channel!.id, candidate.id);
      if (!isCurrent()) return;
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
    run(`dismiss:${candidate.id}`, async (isCurrent) => {
      await studioApi.dismissCandidate(channel!.id, candidate.id);
      if (!isCurrent()) return;
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
      setRewriteInstructions((current) => {
        const next = { ...current };
        delete next[candidate.id];
        return next;
      });
    });

  const restore = (candidate: ContentCandidateView) =>
    run(`restore:${candidate.id}`, async (isCurrent) => {
      await studioApi.restoreCandidate(channel!.id, candidate.id);
      if (!isCurrent()) return;
      setCandidates((current) => current.filter((row) => row.id !== candidate.id));
      setNotice('Материал восстановлен в «Новые»');
    });

  const acceptDraft = (candidate: ContentCandidateView) =>
    run(`draft:${candidate.id}`, async (isCurrent) => {
      const preview = rewritePreviews[candidate.id];
      let draft;
      if (preview?.kind === 'structured') {
        try {
          draft = await applyCurrentStructuredRewrite(channel!.id, candidate.id, preview.runId);
        } catch (reason) {
          if (isCurrent() && isRewriteAuthorityStaleError(reason)) {
            markStructuredPreviewStale(candidate.id, preview.runId);
          }
          throw reason;
        }
      } else {
        draft = await studioApi.candidateDraft(channel!.id, candidate.id);
      }
      if (!isCurrent()) return;
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
      setRewriteInstructions((current) => {
        const next = { ...current };
        delete next[candidate.id];
        return next;
      });
      onOpenContent(draft.id);
    });

  if (!channel) {
    return <div className="sources-empty-page">Выберите канал, чтобы открыть Inbox.</div>;
  }

  const activeScopeKey = inboxStatusScope(statusView);
  const presentation = inboxStatusPresentation(statusView);
  const inboxDataView = resolveScopedDataView(
    loadState,
    channel.id,
    activeScopeKey,
    candidates.length,
  );
  const inboxLoading = inboxDataView === 'loading';
  const inboxLoadFailed = inboxDataView === 'error-without-valid-data';
  const inboxEmpty = inboxDataView === 'loaded-empty';

  return (
    <div className="sources-page inbox-page">
      <header className="sources-topbar">
        <div>
          <small className="eyebrow">{channel.title || channel.tg_chat_id}</small>
          <h1>Inbox</h1>
          <p>Нормализованные кандидаты из Telegram/RSS/Web: анализ → enrichment → policy-safe draft.</p>
        </div>
        <div className="top-actions">
          <button
            className="button secondary"
            onClick={() => void run('refresh', async () => { await load(); })}
            disabled={hasCurrentBusy}
          >
            ↻ Обновить
          </button>
          {presentation.showActiveActions && (
            <button
              className="button secondary"
              onClick={() => void enrichBatch()}
              disabled={hasCurrentBusy || candidates.length === 0}
              title="Deterministic local enrichment, без AI-токенов"
            >
              Local batch
            </button>
          )}
          {isBusy('refresh') && <InlineStatus>Обновляю Inbox…</InlineStatus>}
          {presentation.showActiveActions && isBusy('batch-local') && (
            <InlineStatus>Анализирую Inbox batch…</InlineStatus>
          )}
        </div>
      </header>

      {error?.channelId === channel.id && error.scopeKey === activeScopeKey && (
        <div className="banner error" role="alert">
          {error.message}
          <button aria-label="Закрыть ошибку" onClick={() => setError(null)}>×</button>
        </div>
      )}
      {notice && <InlineStatus className="banner success">{notice}<button aria-label="Закрыть уведомление" onClick={() => setNotice(null)}>×</button></InlineStatus>}

      <section className="sources-inbox-card inbox-standalone-card">
        <div className="panel-heading">
          <div>
            <h2>{presentation.heading}</h2>
            <small>
              {inboxLoading
                ? 'Загружаю Inbox…'
                : inboxLoadFailed
                  ? 'Inbox не загружен'
                  : `${candidates.length} ${presentation.countSuffix}${historyHasMore ? ' · есть более старые' : ''}`}
            </small>
          </div>
          <div className="inbox-status-switch" role="group" aria-label="Фильтр Inbox">
            {INBOX_STATUS_OPTIONS.map((option) => (
              <button
                key={option.status}
                className="button secondary compact"
                aria-pressed={statusView === option.status}
                onClick={() => setStatusView(option.status)}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>
        <AsyncRegion
          className="candidate-list"
          loading={inboxLoading}
          error={inboxLoadFailed}
          empty={inboxEmpty}
          loadingLabel="Загружаю Inbox…"
          loadingFallback={
            <div className="candidate-list-skeleton">
              {[0, 1, 2].map((index) => (
                <div className="candidate-card-skeleton" key={index}>
                  <SkeletonBlock height={20} width="34%" radius={999} />
                  <SkeletonBlock height={14} width={index % 2 ? '62%' : '78%'} />
                  <SkeletonBlock height={10} />
                  <SkeletonBlock height={10} width="86%" />
                  <SkeletonBlock height={30} width="58%" />
                </div>
              ))}
            </div>
          }
          emptyFallback={
            <div className="empty-state">{presentation.emptyText}</div>
          }
          errorFallback={
            <div className="empty-state">Inbox не загружен. Повторите попытку обновления.</div>
          }
        >
          {candidates.map((candidate) => {
            const score = scoreLabel(candidate.score);
            const rewritePreview = rewritePreviews[candidate.id];
            const provenance = rewriteProvenanceLabel(
              rewritePreview,
              candidate.id,
              candidate.source_document_id,
            );
            const media = candidateMedia[candidate.id];
            const canRewrite = presentation.showActiveActions
              && candidate.reuse_policy === 'rewrite_with_attribution';
            const canPromoteMedia = presentation.showActiveActions
              && Boolean(media?.promotable && !media.media_asset_id);
            const prompt = rewriteInstructions[candidate.id] || '';
            const primaryText = candidateInboxPrimaryText(candidate);
            const enrichmentSummary = channelDMEnrichmentSummary(candidate)
              ?? suggestedPostEnrichmentSummary(candidate);
            return (
              <article key={candidate.id} className="candidate-card">
                <div className="candidate-head">
                  <span className="candidate-action">{actionLabel(candidate.suggested_action)}</span>
                  <span className="candidate-policy">{candidate.reuse_policy}</span>
                  {media && <span className="candidate-policy">{candidateMediaLabel(media)}</span>}
                  {score && <span className="candidate-policy">score {score}</span>}
                </div>
                <SuggestedPostProvenance
                  candidateId={candidate.id}
                  suggestedPost={candidate.suggested_post}
                  candidateStatus={candidate.status}
                />
                <ChannelDMProvenance
                  candidateId={candidate.id}
                  channelDM={candidate.channel_dm}
                  candidateStatus={candidate.status}
                />
                <h3>{candidate.topic || candidate.source_title || `Материал #${candidate.source_document_id}`}</h3>
                <p>{primaryText}</p>
                {enrichmentSummary && (
                  <div className="suggested-post-enrichment-summary candidate-enrichment-summary">
                    <small>Enrichment summary</small>
                    <p>{enrichmentSummary}</p>
                  </div>
                )}
                {rewritePreview && (
                  <div className="candidate-rewrite-preview">
                    <small>
                      {provenance || (
                        `AI rewrite preview · run #${rewritePreview.runId}${rewritePreview.model ? ` · ${rewritePreview.model}` : ''}`
                      )}
                    </small>
                    {rewritePreview.document ? (
                      <>
                        <details>
                          <summary>Visual PostDocument preview</summary>
                          <TelegramVisualPreview document={rewritePreview.document} channel={channel} />
                        </details>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
                          {structuredEditLabels.map(([operation, label]) => (
                            <button
                              key={operation}
                              className="button secondary compact"
                              disabled={globalBusy || candidateBusy(candidate.id)}
                              onClick={() => editStructuredAI(candidate, operation)}
                            >
                              {label}
                            </button>
                          ))}
                        </div>
                      </>
                    ) : (
                      <p>{rewritePreview.text}</p>
                    )}
                  </div>
                )}
                {canRewrite && (
                  <div className="candidate-rewrite-preview">
                    <small>Новый Rich proposal по вашему промпту — без автоматического применения</small>
                    <textarea
                      rows={2}
                      maxLength={1000}
                      value={prompt}
                      placeholder="Например: сократи вдвое, добавь 2 подзаголовка и сделай тон спокойнее"
                      onChange={(event) => setRewriteInstructions((current) => ({
                        ...current,
                        [candidate.id]: event.target.value,
                      }))}
                      disabled={globalBusy || candidateBusy(candidate.id)}
                      style={{ width: '100%', boxSizing: 'border-box', marginTop: 8 }}
                    />
                    <button
                      className="button secondary compact"
                      disabled={globalBusy || candidateBusy(candidate.id) || !prompt.trim()}
                      onClick={() => rewritePromptStructuredAI(candidate)}
                    >
                      ✨ Rich по промпту
                    </button>
                  </div>
                )}
                <div className="candidate-footer">
                  <span>{dateLabel(candidate.published_at || candidate.fetched_at)}</span>
                  <div>
                    {candidate.source_url && (
                      <a className="button secondary compact" href={candidate.source_url} target="_blank" rel="noreferrer">Источник ↗</a>
                    )}
                    {presentation.showActiveActions ? (
                      <>
                        {canPromoteMedia && (
                          <button
                            className="button secondary compact"
                            disabled={globalBusy || candidateBusy(candidate.id)}
                            title="Сохранить исходное Telegram media как reusable MediaAsset текущего канала"
                            onClick={() => void promoteMedia(candidate)}
                          >
                            ▣ В медиатеку
                          </button>
                        )}
                        <button
                          className="button secondary compact"
                          disabled={globalBusy || candidateBusy(candidate.id)}
                          onClick={() => void enrichLocal(candidate)}
                        >
                          Local
                        </button>
                        <button
                          className="button secondary compact"
                          disabled={globalBusy || candidateBusy(candidate.id)}
                          title="Использует AI-настройки и лимиты выбранного канала"
                          onClick={() => void enrichAI(candidate)}
                        >
                          ✨ AI
                        </button>
                        {canRewrite && (
                          <button
                            className="button secondary compact"
                            disabled={globalBusy || candidateBusy(candidate.id)}
                            title="Создать независимый AI rewrite; attribution добавит приложение"
                            onClick={() => void rewriteAI(candidate)}
                          >
                            ✨ Rewrite
                          </button>
                        )}
                        {canRewrite && (
                          <button
                            className="button secondary compact"
                            disabled={globalBusy || candidateBusy(candidate.id)}
                            title="Сгенерировать validated Rich PostDocument без автоматического применения"
                            onClick={() => void rewriteStructuredAI(candidate)}
                          >
                            ✨ Rich
                          </button>
                        )}
                        <button
                          className="button primary compact"
                          disabled={globalBusy || candidateBusy(candidate.id)}
                          onClick={() => void acceptDraft(candidate)}
                        >
                          {rewritePreview?.kind === 'structured' ? 'В Rich черновик' : 'В черновик'}
                        </button>
                        <button
                          className="button secondary compact"
                          disabled={globalBusy || candidateBusy(candidate.id)}
                          onClick={() => void dismiss(candidate)}
                        >
                          Скрыть
                        </button>
                      </>
                    ) : (
                      <button
                        className="button primary compact"
                        disabled={globalBusy || candidateBusy(candidate.id)}
                        onClick={() => void restore(candidate)}
                      >
                        Восстановить
                      </button>
                    )}
                  </div>
                </div>
                <InlineStatus className="candidate-operation-status">
                  {candidateOperationLabel(candidate.id)}
                </InlineStatus>
              </article>
            );
          })}
          {(historyHasMore || historyLoadingMore || historyLoadMoreError) && (
            <div className="inbox-history-controls">
              {historyLoadMoreError && (
                <div className="inbox-history-error" role="alert">
                  Не удалось загрузить старые материалы: {historyLoadMoreError}
                </div>
              )}
              {historyHasMore && (
                <button
                  className="button secondary compact"
                  onClick={() => void loadOlder()}
                  disabled={historyLoadingMore || globalBusy}
                >
                  {historyLoadingMore
                    ? 'Загружаю старые…'
                    : historyLoadMoreError
                      ? 'Повторить загрузку'
                      : 'Показать старые'}
                </button>
              )}
              {historyLoadingMore && (
                <InlineStatus>Загружаю более старые материалы…</InlineStatus>
              )}
            </div>
          )}
        </AsyncRegion>
      </section>
    </div>
  );
}
