import { useCallback, useEffect, useRef, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import type {
  AssistantApprovalView,
  AssistantDraftReference,
  AssistantRunView,
  AssistantScenario,
} from './api';
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

function scenarioLabel(value: AssistantScenario): string {
  return value === 'drafts_tomorrow' ? '3 черновика на завтра' : 'Внимание сегодня';
}

function approvalStateLabel(value: string): string {
  if (value === 'pending_review') return 'Ожидает подтверждения';
  if (value === 'executing') return 'Выполняется';
  if (value === 'executed') return 'Поставлено в план';
  if (value === 'rejected') return 'Отклонено';
  if (value === 'stale') return 'Устарело';
  if (value === 'failed') return 'Ошибка выполнения';
  return value;
}

function newRequestId(prefix: string): string {
  const randomUUID = globalThis.crypto?.randomUUID?.bind(globalThis.crypto);
  if (randomUUID) return `${prefix}-${randomUUID()}`;
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

function latestApprovalForDraft(
  approvals: AssistantApprovalView[],
  contentItemId: number,
): AssistantApprovalView | null {
  return approvals.find((row) => row.content_item_id === contentItemId) ?? null;
}

export function mergeAssistantRun(
  previous: AssistantRunView[],
  run: AssistantRunView,
  limit = 10,
): AssistantRunView[] {
  return [run, ...previous.filter((item) => item.id !== run.id)].slice(0, limit);
}

export function mergeAssistantApproval(
  previous: AssistantApprovalView[],
  approval: AssistantApprovalView,
  limit = 50,
): AssistantApprovalView[] {
  return [approval, ...previous.filter((item) => item.id !== approval.id)].slice(0, limit);
}

function DraftApprovalCard({
  draft,
  targetLocalDate,
  timezone,
  approval,
  proposalBusy,
  reviewBusy,
  onOpenContent,
  onOpenPlanner,
  onCreateProposal,
  onApprove,
  onReject,
}: {
  draft: AssistantDraftReference;
  targetLocalDate: string;
  timezone: string;
  approval: AssistantApprovalView | null;
  proposalBusy: boolean;
  reviewBusy: boolean;
  onOpenContent?: (contentId: number) => void;
  onOpenPlanner?: VoidFunction;
  onCreateProposal?: (contentId: number, localTime: string) => Promise<void>;
  onApprove?: (approvalId: number) => Promise<void>;
  onReject?: (approvalId: number) => Promise<void>;
}) {
  const [localTime, setLocalTime] = useState('14:30');
  const statusRef = useRef<HTMLDivElement | null>(null);
  const previousStateRef = useRef<string | null>(approval?.state ?? null);
  const approvalState = approval?.state ?? null;
  const canCreate = !approval || ['rejected', 'stale', 'failed'].includes(approval.state);
  const canReview = approval?.state === 'pending_review';
  const canRecover = approval?.state === 'executing';

  useEffect(() => {
    const previous = previousStateRef.current;
    previousStateRef.current = approvalState;
    if (
      previous
      && previous !== approvalState
      && approvalState
      && ['executed', 'rejected', 'stale', 'failed'].includes(approvalState)
    ) {
      statusRef.current?.focus();
    }
  }, [approvalState]);

  return (
    <article className="assistant-draft-card">
      <div className="assistant-item-heading">
        <strong>{draft.title}</strong>
        <span className="assistant-run-status assistant-run-status-completed">Черновик</span>
      </div>
      <small>
        {targetLocalDate} · Content #{draft.content_item_id} · revision {draft.content_revision}
      </small>
      <div className="assistant-refs">
        <button
          className="link-button"
          onClick={() => onOpenContent?.(draft.content_item_id)}
        >
          Открыть в редакторе
        </button>
      </div>

      {approval && (
        <section className="assistant-approval-card" aria-label="Предложение постановки в план">
          <div
            className={`assistant-approval-status assistant-approval-status-${approval.state}`}
            role="status"
            tabIndex={-1}
            ref={statusRef}
          >
            <strong>{approvalStateLabel(approval.state)}</strong>
            <small>Approval #{approval.id}</small>
          </div>
          <dl className="assistant-approval-facts">
            <div>
              <dt>Контент</dt>
              <dd>Content #{approval.content_item_id}, revision {approval.content_revision}</dd>
            </div>
            <div>
              <dt>Локальное время</dt>
              <dd>{approval.target_local_date} · {approval.local_time} · {approval.timezone}</dd>
            </div>
            <div>
              <dt>Точный effect</dt>
              <dd>Будут созданы ScheduleEntry + Publication</dd>
            </div>
          </dl>
          <p className="assistant-approval-note">
            Telegram сейчас не отправляется. Отправка будет выполняться canonical delivery worker
            в запланированное время.
          </p>
          {approval.failure_reason && (
            <p className="assistant-approval-error" role={approval.state === 'failed' ? 'alert' : undefined}>
              {approval.failure_reason}
            </p>
          )}
          {canReview && (
            <div className="assistant-approval-actions">
              <button
                className="button primary assistant-important-action"
                disabled={reviewBusy}
                onClick={() => void onApprove?.(approval.id)}
              >
                {reviewBusy ? 'Подтверждаю…' : 'Подтвердить постановку в план'}
              </button>
              <button
                className="button secondary"
                disabled={reviewBusy}
                onClick={() => void onReject?.(approval.id)}
              >
                Отклонить
              </button>
            </div>
          )}
          {canRecover && (
            <div className="assistant-approval-actions">
              <button
                className="button secondary"
                disabled={reviewBusy}
                onClick={() => void onApprove?.(approval.id)}
              >
                {reviewBusy ? 'Проверяю…' : 'Проверить выполнение'}
              </button>
            </div>
          )}
          {approval.state === 'executed' && (
            <div className="assistant-executed-result">
              <span>ScheduleEntry #{approval.schedule_entry_id}</span>
              <span>Publication #{approval.publication_id}</span>
              <span>{approval.target_local_date} · {approval.local_time} · {approval.timezone}</span>
              <button className="link-button" onClick={onOpenPlanner}>
                Открыть Planner
              </button>
            </div>
          )}
        </section>
      )}

      {canCreate && (
        <div className="assistant-proposal-form">
          <label htmlFor={`assistant-time-${draft.content_item_id}`}>
            Локальное время на завтра
          </label>
          <div className="assistant-proposal-controls">
            <input
              id={`assistant-time-${draft.content_item_id}`}
              type="time"
              value={localTime}
              onChange={(event) => setLocalTime(event.target.value)}
              disabled={proposalBusy}
              required
            />
            <button
              className="button secondary"
              disabled={proposalBusy || !onCreateProposal || !localTime}
              onClick={() => void onCreateProposal?.(draft.content_item_id, localTime)}
            >
              {proposalBusy ? 'Создаю предложение…' : 'Создать предложение'}
            </button>
          </div>
          <small>
            На этом шаге расписание не меняется: сначала будет создана review-карточка.
          </small>
        </div>
      )}
    </article>
  );
}

export function AssistantBrief({
  run,
  approvals = [],
  busyKeys = new Set<string>(),
  onOpenPlanner,
  onOpenContent,
  onCreateProposal,
  onApprove,
  onReject,
}: {
  run: AssistantRunView;
  approvals?: AssistantApprovalView[];
  busyKeys?: Set<string>;
  onOpenPlanner?: VoidFunction;
  onOpenContent?: (contentId: number) => void;
  onCreateProposal?: (contentId: number, localTime: string) => Promise<void>;
  onApprove?: (approvalId: number) => Promise<void>;
  onReject?: (approvalId: number) => Promise<void>;
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

  if (result.scenario === 'drafts_tomorrow') {
    return (
      <div className="assistant-brief">
        <div className="assistant-summary">
          <small>{result.timezone} · редакционный день {result.target_local_date}</small>
          <p>
            Создано {result.draft_count} обычных черновика Content domain.
            Расписание и публикация не создаются до явного подтверждения отдельного предложения.
          </p>
        </div>
        <div className="assistant-drafts">
          {result.drafts.map((draft) => {
            const approval = latestApprovalForDraft(approvals, draft.content_item_id);
            const proposalBusy = busyKeys.has(`proposal:${draft.content_item_id}`);
            const reviewBusy = approval
              ? busyKeys.has(`approval:${approval.id}`)
              : false;
            return (
              <DraftApprovalCard
                key={draft.content_item_id}
                draft={draft}
                targetLocalDate={result.target_local_date}
                timezone={result.timezone}
                approval={approval}
                proposalBusy={proposalBusy}
                reviewBusy={reviewBusy}
                onOpenContent={onOpenContent}
                onOpenPlanner={onOpenPlanner}
                onCreateProposal={onCreateProposal}
                onApprove={onApprove}
                onReject={onReject}
              />
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div className="assistant-brief">
      <div className="assistant-summary">
        <small>
          {result.timezone} · {result.generated_by === 'llm_priority' ? 'AI-prioritized facts' : 'deterministic facts'}
        </small>
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
  const [approvals, setApprovals] = useState<AssistantApprovalView[]>([]);
  const [currentRun, setCurrentRun] = useState<AssistantRunView | null>(null);
  const [loadState, setLoadState] = useState<ChannelLoadState | null>(null);
  const [error, setError] = useState<{ channelId: number; message: string } | null>(null);
  const [runningScenario, setRunningScenario] = useState<AssistantScenario | null>(null);
  const [busyApprovalKeys, setBusyApprovalKeys] = useState<Set<string>>(() => new Set());
  const requestOwnershipRef = useRef(new ChannelRequestOwnership());
  const runOwnershipRef = useRef(new ChannelRequestOwnership());
  const operationLockRef = useRef(new ExclusiveOperationLock());
  const approvalLocksRef = useRef(new Map<string, ExclusiveOperationLock>());
  const validDataChannelRef = useRef<number | null>(null);
  const channelIdRef = useRef<number | null>(channel?.id ?? null);
  channelIdRef.current = channel?.id ?? null;

  const loadHistory = useCallback(async () => {
    const channelId = channelIdRef.current;
    if (channelId === null) {
      requestOwnershipRef.current.invalidate();
      validDataChannelRef.current = null;
      setRuns([]);
      setApprovals([]);
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
      setApprovals([]);
      setCurrentRun(null);
      setLoadState({ channelId, phase: 'loading' });
    }
    setError(null);
    try {
      const [rows, approvalRows] = await Promise.all([
        studioApi.assistantRuns(channelId, 10),
        studioApi.assistantApprovals(channelId, 50),
      ]);
      if (!isCurrent()) return;
      validDataChannelRef.current = channelId;
      setRuns(rows);
      setApprovals(approvalRows);
      setCurrentRun((existing) => (
        existing?.channel_id === channelId ? existing : rows[0] ?? null
      ));
      setLoadState({ channelId, phase: 'loaded' });
    } catch (reason) {
      if (!isCurrent()) return;
      if (validDataChannelRef.current !== channelId) {
        setRuns([]);
        setApprovals([]);
        setCurrentRun(null);
        setLoadState({ channelId, phase: 'error-without-valid-data' });
      }
      setError({ channelId, message: errorMessage(reason) });
    }
  }, [channel?.id]);

  useEffect(() => {
    runOwnershipRef.current.invalidate();
    approvalLocksRef.current.clear();
    setBusyApprovalKeys(new Set());
    setRunningScenario(null);
    void loadHistory();
  }, [loadHistory]);

  const runScenario = useCallback(async (scenario: AssistantScenario) => {
    const channelId = channelIdRef.current;
    if (channelId === null) return;
    const result = await runExclusiveOperation(
      operationLockRef.current,
      scenario,
      async () => {
        const token = runOwnershipRef.current.begin(channelId);
        const isCurrent = () => runOwnershipRef.current.isCurrent(token, channelIdRef.current);
        setRunningScenario(scenario);
        setError(null);
        try {
          const requestId = scenario === 'drafts_tomorrow' ? newRequestId('draft') : null;
          const run = await studioApi.createAssistantRun(channelId, scenario, requestId);
          if (!isCurrent()) return;
          setCurrentRun(run);
          setRuns((previous) => mergeAssistantRun(previous, run));
          validDataChannelRef.current = channelId;
          setLoadState({ channelId, phase: 'loaded' });
        } catch (reason) {
          if (isCurrent()) {
            setError({ channelId, message: errorMessage(reason) });
          }
        } finally {
          if (isCurrent()) setRunningScenario(null);
        }
      },
    );
    if (!result.started) return;
  }, []);

  const runApprovalOperation = useCallback(async (
    key: string,
    operation: (channelId: number) => Promise<AssistantApprovalView>,
  ) => {
    const channelId = channelIdRef.current;
    if (channelId === null) return;
    const scopedKey = `${channelId}:${key}`;
    let lock = approvalLocksRef.current.get(scopedKey);
    if (!lock) {
      lock = new ExclusiveOperationLock();
      approvalLocksRef.current.set(scopedKey, lock);
    }
    const result = await runExclusiveOperation(lock, scopedKey, async () => {
      setBusyApprovalKeys((previous) => new Set(previous).add(key));
      setError(null);
      try {
        const approval = await operation(channelId);
        if (channelIdRef.current !== channelId) return approval;
        setApprovals((previous) => mergeAssistantApproval(previous, approval));
        return approval;
      } catch (reason) {
        if (channelIdRef.current === channelId) {
          setError({ channelId, message: errorMessage(reason) });
        }
        return null;
      } finally {
        if (channelIdRef.current === channelId) {
          setBusyApprovalKeys((previous) => {
            const next = new Set(previous);
            next.delete(key);
            return next;
          });
        }
      }
    });
    if (!result.started) return;
  }, []);

  const createProposal = useCallback(async (contentId: number, localTime: string) => {
    await runApprovalOperation(`proposal:${contentId}`, (channelId) =>
      studioApi.createAssistantApproval(
        channelId,
        contentId,
        localTime,
        newRequestId('approval'),
      ));
  }, [runApprovalOperation]);

  const approve = useCallback(async (approvalId: number) => {
    await runApprovalOperation(`approval:${approvalId}`, (channelId) =>
      studioApi.approveAssistantApproval(channelId, approvalId));
  }, [runApprovalOperation]);

  const reject = useCallback(async (approvalId: number) => {
    await runApprovalOperation(`approval:${approvalId}`, (channelId) =>
      studioApi.rejectAssistantApproval(channelId, approvalId));
  }, [runApprovalOperation]);

  if (!channel) {
    return <div className="sources-empty-page">Выберите канал, чтобы открыть Assistant.</div>;
  }

  const dataView = resolveChannelDataView(loadState, channel.id, runs.length);
  const initialLoading = dataView === 'loading';
  const loadFailed = dataView === 'error-without-valid-data';
  const loadedEmpty = dataView === 'loaded-empty';
  const running = runningScenario !== null;

  return (
    <div className="assistant-page">
      <header className="sources-topbar">
        <div>
          <small className="eyebrow">{channel.title || channel.tg_chat_id}</small>
          <h1>Assistant</h1>
          <p>
            Bounded work panel: attention flow читает состояние, draft flow создаёт обычные
            черновики, а постановка в план выполняется только через явное approval.
          </p>
        </div>
        <div className="top-actions">
          <button
            className="button primary"
            onClick={() => void runScenario('attention_today')}
            disabled={running}
          >
            Что сегодня требует внимания?
          </button>
          <button
            className="button secondary"
            onClick={() => void runScenario('drafts_tomorrow')}
            disabled={running}
          >
            Создать 3 черновика на завтра
          </button>
          {runningScenario === 'attention_today' && (
            <InlineStatus>Собираю проверяемый snapshot…</InlineStatus>
          )}
          {runningScenario === 'drafts_tomorrow' && (
            <InlineStatus>Готовлю три черновика в стиле канала…</InlineStatus>
          )}
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
            <h2>Assistant result</h2>
            <small>
              {currentRun
                ? `run #${currentRun.id} · ${scenarioLabel(currentRun.scenario)} · ${currentRun.status}`
                : 'Запустите один из bounded сценариев'}
            </small>
          </div>
        </div>
        {running && !currentRun ? (
          <div className="assistant-brief-skeleton">
            <SkeletonBlock height={13} width="62%" />
            <SkeletonBlock height={11} width="88%" />
            <SkeletonBlock height={72} />
          </div>
        ) : currentRun ? (
          <AssistantBrief
            run={currentRun}
            approvals={approvals}
            busyKeys={busyApprovalKeys}
            onOpenPlanner={onOpenPlanner}
            onOpenContent={onOpenContent}
            onCreateProposal={createProposal}
            onApprove={approve}
            onReject={reject}
          />
        ) : (
          <div className="assistant-empty">
            Результата пока нет. Каждый CTA запускает один bounded request.
          </div>
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
                <strong>{scenarioLabel(run.scenario)} · run #{run.id}</strong>
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
