import { useCallback, useEffect, useRef, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import type { AssistantAttentionItem, AssistantRunView } from './api';
import { AsyncRegion, InlineStatus, SkeletonBlock } from './AsyncUI';
import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveChannelDataView,
  runExclusiveOperation,
  type ChannelLoadState,
} from './asyncControl';
import type { Channel } from './types';

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

function severityLabel(value: string): string {
  if (value === 'critical' || value === 'high') return 'Высокий';
  if (value === 'medium') return 'Средний';
  if (value === 'low') return 'Низкий';
  return 'Инфо';
}

export function AssistantBrief({
  run,
  onOpenPlanner,
  onOpenContent,
}: {
  run: AssistantRunView;
  onOpenPlanner?: VoidFunction;
  onOpenContent?: (contentId: number) => void;
}) {
  const result = run.result;
  if (run.status === 'failed') {
    return (
      <div className="assistant-run-error" role="alert">
        Не удалось завершить запуск. {run.error || 'Повторите попытку.'}
      </div>
    );
  }
  if (!result) return <div className="assistant-empty">Результат ещё не готов.</div>;

  return (
    <div className="assistant-brief">
      <div className="assistant-summary">
        <small>{result.timezone} · {result.generated_by === 'llm' ? 'AI summary' : 'deterministic summary'}</small>
        <p>{result.summary}</p>
      </div>
      {result.attention_items.length === 0 ? (
        <div className="assistant-empty">Проверяемых предупреждений в snapshot нет.</div>
      ) : (
        <div className="assistant-items">
          {result.attention_items.map((item) => (
            <article className="assistant-item" key={item.fact_id}>
              <div className="assistant-item-heading">
                <strong>{item.title}</strong>
                <span className={`assistant-severity assistant-severity-${item.severity}`}>
                  {severityLabel(item.severity)}
                </span>
              </div>
              <p>{item.detail}</p>
              <small>{item.suggested_action}</small>
              <div className="assistant-refs">
                {item.refs.schedule_entry_id !== undefined && (
                  <button className="link-button" onClick={onOpenPlanner}>
                    ScheduleEntry #{item.refs.schedule_entry_id}
                  </button>
                )}
                {item.refs.publication_id !== undefined && (
                  <span>Publication #{item.refs.publication_id}</span>
                )}
                {item.refs.source_connector_id !== undefined && (
                  <span>Source #{item.refs.source_connector_id}</span>
                )}
                {item.refs.content_item_id !== undefined && (
                  <button
                    className="link-button"
                    onClick={() => onOpenContent?.(item.refs.content_item_id!)}
                  >
                    Content #{item.refs.content_item_id}
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

export function AssistantPanel({
  channel,
  onOpenPlanner,
  onOpenContent,
}: {
  channel: Channel | null;
  onOpenPlanner?: VoidFunction;
  onOpenContent?: (contentId: number) => void;
}) {
  const [runs, setRuns] = useState<AssistantRunView[]>([]);
  const [currentRun, setCurrentRun] = useState<AssistantRunView | null>(null);
  const [loadState, setLoadState] = useState<ChannelLoadState | null>(null);
  const [error, setError] = useState<{ channelId: number; message: string } | null>(null);
  const [running, setRunning] = useState(false);
  const requestOwnershipRef = useRef(new ChannelRequestOwnership());
  const operationLockRef = useRef(new ExclusiveOperationLock());
  const validDataChannelRef = useRef<number | null>(null);
  const channelIdRef = useRef<number | null>(channel?.id ?? null);
  channelIdRef.current = channel?.id ?? null;

  const loadHistory = useCallback(async () => {
    const channelId = channelIdRef.current;
    if (channelId === null) {
      requestOwnershipRef.current.invalidate();
      validDataChannelRef.current = null;
      setRuns([]);
      setCurrentRun(null);
      setLoadState(null);
      setError(null);
      return;
    }
    const token = requestOwnershipRef.current.begin(channelId);
    const hasValidData = validDataChannelRef.current === channelId;
    const isCurrent = () => requestOwnershipRef.current.isCurrent(token, channelIdRef.current);
    if (!hasValidData) {
      validDataChannelRef.current = null;
      setRuns([]);
      setCurrentRun(null);
      setLoadState({ channelId, phase: 'loading' });
    }
    setError(null);
    try {
      const rows = await studioApi.assistantRuns(channelId, 10);
      if (!isCurrent()) return;
      validDataChannelRef.current = channelId;
      setRuns(rows);
      setCurrentRun((existing) => (
        existing?.channel_id === channelId ? existing : rows[0] ?? null
      ));
      setLoadState({ channelId, phase: 'loaded' });
    } catch (reason) {
      if (!isCurrent()) return;
      if (validDataChannelRef.current !== channelId) {
        setRuns([]);
        setCurrentRun(null);
        setLoadState({ channelId, phase: 'error-without-valid-data' });
      }
      setError({ channelId, message: errorMessage(reason) });
    }
  }, [channel?.id]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  const runAttention = useCallback(async () => {
    const channelId = channelIdRef.current;
    if (channelId === null) return;
    const result = await runExclusiveOperation(
      operationLockRef.current,
      'attention_today',
      async () => {
        setRunning(true);
        setError(null);
        try {
          const run = await studioApi.createAssistantRun(channelId, 'attention_today');
          if (channelIdRef.current !== channelId) return;
          setCurrentRun(run);
          setRuns((previous) => [run, ...previous.filter((item) => item.id !== run.id)].slice(0, 10));
          validDataChannelRef.current = channelId;
          setLoadState({ channelId, phase: 'loaded' });
        } catch (reason) {
          if (channelIdRef.current === channelId) {
            setError({ channelId, message: errorMessage(reason) });
          }
        } finally {
          if (channelIdRef.current === channelId) setRunning(false);
        }
      },
    );
    if (!result.started) return;
  }, []);

  if (!channel) {
    return <div className="sources-empty-page">Выберите канал, чтобы открыть Assistant.</div>;
  }

  const dataView = resolveChannelDataView(loadState, channel.id, runs.length);
  const initialLoading = dataView === 'loading';
  const loadFailed = dataView === 'error-without-valid-data';
  const loadedEmpty = dataView === 'loaded-empty';

  return (
    <div className="assistant-page">
      <header className="sources-topbar">
        <div>
          <small className="eyebrow">{channel.title || channel.tg_chat_id}</small>
          <h1>Assistant</h1>
          <p>Bounded operational work panel. MVP A только читает состояние канала и ничего не публикует.</p>
        </div>
        <div className="top-actions">
          <button className="button primary" onClick={() => void runAttention()} disabled={running}>
            Что сегодня требует внимания?
          </button>
          {running && <InlineStatus>Собираю проверяемый snapshot…</InlineStatus>}
        </div>
      </header>

      {error?.channelId === channel.id && (
        <div className="banner error" role="alert">
          {error.message}
          <button onClick={() => setError(null)}>×</button>
        </div>
      )}

      <section className="assistant-work-card">
        <div className="panel-heading">
          <div>
            <h2>Operational brief</h2>
            <small>{currentRun ? `run #${currentRun.id} · ${currentRun.status}` : 'Запустите read-only проверку'}</small>
          </div>
        </div>
        {running && !currentRun ? (
          <div className="assistant-brief-skeleton">
            <SkeletonBlock height={13} width="62%" />
            <SkeletonBlock height={11} width="88%" />
            <SkeletonBlock height={72} />
          </div>
        ) : currentRun ? (
          <AssistantBrief run={currentRun} onOpenPlanner={onOpenPlanner} onOpenContent={onOpenContent} />
        ) : (
          <div className="assistant-empty">Результата пока нет. CTA запускает один bounded request.</div>
        )}
      </section>

      <section className="assistant-history-card">
        <div className="panel-heading">
          <div>
            <h2>Recent runs</h2>
            <small>{initialLoading ? 'Загружаю историю…' : `${runs.length} последних запусков`}</small>
          </div>
        </div>
        <AsyncRegion
          className="assistant-history"
          loading={initialLoading}
          error={loadFailed}
          empty={loadedEmpty}
          loadingLabel="Загружаю историю Assistant…"
          loadingFallback={
            <>
              {[0, 1, 2].map((index) => (
                <div className="assistant-history-row" key={index}>
                  <SkeletonBlock height={12} width={index ? '48%' : '60%'} />
                  <SkeletonBlock height={9} width="36%" />
                </div>
              ))}
            </>
          }
          emptyFallback={<div className="assistant-empty">История запусков пока пуста.</div>}
          errorFallback={<div className="assistant-empty">История Assistant не загружена.</div>}
        >
          {runs.map((run) => (
            <button
              className={currentRun?.id === run.id ? 'assistant-history-row selected' : 'assistant-history-row'}
              key={run.id}
              onClick={() => setCurrentRun(run)}
            >
              <span>
                <strong>run #{run.id}</strong>
                <small>{dateLabel(run.finished_at || run.created_at)}</small>
              </span>
              <span className={`assistant-run-status assistant-run-status-${run.status}`}>{run.status}</span>
            </button>
          ))}
        </AsyncRegion>
      </section>
    </div>
  );
}
