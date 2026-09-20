import { useCallback, useEffect, useRef, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import type {
  AssistantApprovalView,
  AssistantContentSeriesOperatorInput,
  AssistantContentSeriesRunResult,
  AssistantDraftReference,
  AssistantRunView,
  AssistantScenario,
  AssistantSeriesApprovalSlotInput,
  AssistantSeriesApprovalView,
  AssistantSkillView,
} from './api';
import { AsyncRegion, InlineStatus, SkeletonBlock } from './AsyncUI';
import { AssistantAutomations } from './AssistantAutomations';
import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveChannelDataView,
  resolveScopedDataView,
  runExclusiveOperation,
  type ChannelLoadState,
  type ChannelRequestToken,
  type ScopedLoadState,
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
  if (value === 'drafts_tomorrow') return '3 черновика на завтра';
  if (value === 'prepare_content_series') return 'Серия постов';
  return 'Внимание сегодня';
}

function approvalStateLabel(value: string): string {
  if (value === 'pending_review') return 'Ожидает подтверждения';
  if (value === 'executing') return 'Выполняется';
  if (value === 'executed') return 'Поставлено в план';
  if (value === 'rejected') return 'Отклонено';
  if (value === 'stale') return 'Устарело';
  if (value === 'partial_failed') return 'Частично выполнено';
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


function latestSeriesApproval(
  approvals: AssistantSeriesApprovalView[],
  sourceRunId: number,
): AssistantSeriesApprovalView | null {
  return approvals.find((row) => row.source_run_id === sourceRunId) ?? null;
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
  limit = 8,
): AssistantApprovalView[] {
  return [
    approval,
    ...previous.filter(
      (item) => item.id !== approval.id && item.content_item_id !== approval.content_item_id,
    ),
  ].slice(0, limit);
}


export function mergeAssistantSeriesApproval(
  previous: AssistantSeriesApprovalView[],
  approval: AssistantSeriesApprovalView,
  limit = 1,
): AssistantSeriesApprovalView[] {
  return [
    approval,
    ...previous.filter(
      (item) => item.id !== approval.id && item.source_run_id !== approval.source_run_id,
    ),
  ].slice(0, limit);
}

function DraftApprovalCard({
  draft,
  targetLocalDate,
  timezone,
  approval,
  associationReady,
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
  associationReady: boolean;
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
  const canCreate = associationReady
    && (!approval || ['rejected', 'stale', 'failed'].includes(approval.state));
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


function SeriesApprovalSection({
  runId,
  result,
  approval,
  associationReady,
  proposalBusy,
  reviewBusy,
  onOpenPlanner,
  onCreateProposal,
  onApprove,
  onReject,
}: {
  runId: number;
  result: AssistantContentSeriesRunResult;
  approval: AssistantSeriesApprovalView | null;
  associationReady: boolean;
  proposalBusy: boolean;
  reviewBusy: boolean;
  onOpenPlanner?: VoidFunction;
  onCreateProposal?: (
    sourceRunId: number,
    slots: AssistantSeriesApprovalSlotInput[],
  ) => Promise<void>;
  onApprove?: (batchId: number) => Promise<void>;
  onReject?: (batchId: number) => Promise<void>;
}) {
  const [slots, setSlots] = useState<AssistantSeriesApprovalSlotInput[]>(() =>
    result.posts.map((post) => ({
      ordinal: post.ordinal,
      local_date: '',
      local_time: '',
    })));
  const statusRef = useRef<HTMLDivElement | null>(null);
  const previousStateRef = useRef<string | null>(approval?.state ?? null);
  const approvalState = approval?.state ?? null;
  const canCreate = associationReady
    && (!approval || ['rejected', 'stale', 'failed'].includes(approval.state));
  const canReview = approval?.state === 'pending_review';
  const canRecover = approval?.state === 'executing';
  const slotsComplete = slots.length === result.requested_post_count
    && slots.every((slot) => slot.local_date && slot.local_time);

  useEffect(() => {
    const previous = previousStateRef.current;
    previousStateRef.current = approvalState;
    if (
      previous
      && previous !== approvalState
      && approvalState
      && ['executed', 'rejected', 'stale', 'partial_failed', 'failed'].includes(approvalState)
    ) {
      statusRef.current?.focus();
    }
  }, [approvalState]);

  const updateSlot = (
    ordinal: number,
    field: 'local_date' | 'local_time',
    value: string,
  ) => {
    setSlots((previous) => previous.map((slot) => (
      slot.ordinal === ordinal ? { ...slot, [field]: value } : slot
    )));
  };

  return (
    <section className="assistant-series-scheduling" aria-label="Постановка серии в план">
      <div className="assistant-series-scheduling-heading">
        <div>
          <h3>Расписание серии</h3>
          <small>
            Для каждого поста задайте явные локальные дату и время. Создание предложения
            не меняет canonical schedule.
          </small>
        </div>
      </div>

      {approval && (
        <section className="assistant-approval-card assistant-series-review-card" aria-label="Review расписания серии">
          <div
            className={`assistant-approval-status assistant-approval-status-${approval.state}`}
            role="status"
            tabIndex={-1}
            ref={statusRef}
          >
            <strong>{approvalStateLabel(approval.state)}</strong>
            <small>Batch approval #{approval.id}</small>
          </div>
          <dl className="assistant-approval-facts">
            <div>
              <dt>Серия</dt>
              <dd>{approval.series_title}</dd>
            </div>
            <div>
              <dt>Источник</dt>
              <dd>run #{approval.source_run_id} · fingerprint {approval.source_plan_fingerprint.slice(0, 12)}</dd>
            </div>
            <div>
              <dt>План</dt>
              <dd>{approval.item_count} постов · {approval.timezone}</dd>
            </div>
          </dl>
          <p className="assistant-approval-note">
            После подтверждения будет создано {approval.item_count} canonical ScheduleEntry +{' '}
            {approval.item_count} Publication. Telegram сейчас не отправляется; доставка выполняется
            существующим canonical worker в назначенное время.
          </p>
          <div className="assistant-series-review-items">
            {approval.items.map((item) => (
              <article className="assistant-series-review-item" key={item.id}>
                <div className="assistant-item-heading">
                  <strong>{item.ordinal}. {item.content_title}</strong>
                  <span className={`assistant-run-status assistant-run-status-${item.state}`}>
                    {item.state}
                  </span>
                </div>
                <small>
                  Content #{item.content_item_id} · captured revision {item.captured_content_revision}
                </small>
                <small>
                  {item.local_date} · {item.local_time} · {approval.timezone}
                </small>
                {(item.schedule_entry_id !== null || item.publication_id !== null) && (
                  <div className="assistant-executed-result">
                    {item.schedule_entry_id !== null && (
                      <span>ScheduleEntry #{item.schedule_entry_id}</span>
                    )}
                    {item.publication_id !== null && (
                      <span>Publication #{item.publication_id}</span>
                    )}
                  </div>
                )}
                {item.failure_reason && (
                  <p className="assistant-approval-error">{item.failure_reason}</p>
                )}
              </article>
            ))}
          </div>
          {approval.failure_reason && (
            <p
              className="assistant-approval-error"
              role={approval.state === 'failed' || approval.state === 'partial_failed' ? 'alert' : undefined}
            >
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
                {reviewBusy ? 'Подтверждаю…' : 'Подтвердить постановку серии в план'}
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
          {['executed', 'partial_failed'].includes(approval.state) && (
            <div className="assistant-approval-actions">
              <button className="link-button" onClick={onOpenPlanner}>
                Открыть Planner
              </button>
            </div>
          )}
        </section>
      )}

      {canCreate && (
        <div className="assistant-series-proposal-form">
          <div className="assistant-series-slot-grid">
            {result.posts.map((post) => {
              const slot = slots.find((row) => row.ordinal === post.ordinal);
              return (
                <fieldset className="assistant-series-slot" key={post.ordinal}>
                  <legend>{post.ordinal}. {post.title}</legend>
                  <label>
                    Локальная дата
                    <input
                      type="date"
                      value={slot?.local_date ?? ''}
                      onChange={(event) => updateSlot(post.ordinal, 'local_date', event.target.value)}
                      disabled={proposalBusy}
                      required
                    />
                  </label>
                  <label>
                    Локальное время
                    <input
                      type="time"
                      value={slot?.local_time ?? ''}
                      onChange={(event) => updateSlot(post.ordinal, 'local_time', event.target.value)}
                      disabled={proposalBusy}
                      required
                    />
                  </label>
                </fieldset>
              );
            })}
          </div>
          <div className="assistant-approval-actions">
            <button
              className="button secondary"
              disabled={proposalBusy || !slotsComplete || !onCreateProposal}
              onClick={() => void onCreateProposal?.(runId, slots)}
            >
              {proposalBusy ? 'Создаю предложение…' : 'Создать предложение расписания'}
            </button>
          </div>
          <small>
            На этом шаге ScheduleEntry и Publication не создаются; сначала появится review-карточка.
          </small>
        </div>
      )}
    </section>
  );
}

export function AssistantBrief({
  run,
  approvals = [],
  draftApprovalsReady = true,
  seriesApprovals = [],
  seriesApprovalsReady = true,
  busyKeys = new Set<string>(),
  onOpenPlanner,
  onOpenContent,
  onCreateProposal,
  onApprove,
  onReject,
  onCreateSeriesProposal,
  onApproveSeries,
  onRejectSeries,
}: {
  run: AssistantRunView;
  approvals?: AssistantApprovalView[];
  draftApprovalsReady?: boolean;
  seriesApprovals?: AssistantSeriesApprovalView[];
  seriesApprovalsReady?: boolean;
  busyKeys?: Set<string>;
  onOpenPlanner?: VoidFunction;
  onOpenContent?: (contentId: number) => void;
  onCreateProposal?: (contentId: number, localTime: string) => Promise<void>;
  onApprove?: (approvalId: number) => Promise<void>;
  onReject?: (approvalId: number) => Promise<void>;
  onCreateSeriesProposal?: (
    sourceRunId: number,
    slots: AssistantSeriesApprovalSlotInput[],
  ) => Promise<void>;
  onApproveSeries?: (batchId: number) => Promise<void>;
  onRejectSeries?: (batchId: number) => Promise<void>;
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

  if (result.scenario === 'prepare_content_series') {
    return (
      <div className="assistant-brief">
        <div className="assistant-summary">
          <small>
            Series plan · {result.requested_post_count} posts · fingerprint{' '}
            {result.plan_fingerprint.slice(0, 12)}
          </small>
          <strong>{result.series_title}</strong>
          <p>{result.series_summary}</p>
          {result.editorial_context && (
            <small>
              Bounded context: {result.editorial_context.recent_count} recent ·{' '}
              {result.editorial_context.scheduled_count} scheduled refs
            </small>
          )}
        </div>
        <div className="assistant-drafts">
          {result.posts.map((post) => (
            <article className="assistant-draft-card" key={post.content_item_id}>
              <div className="assistant-item-heading">
                <strong>{post.ordinal}. {post.title}</strong>
                <span className="assistant-run-status assistant-run-status-completed">
                  Черновик
                </span>
              </div>
              <p><strong>Угол:</strong> {post.angle}</p>
              <p><strong>Цель:</strong> {post.objective}</p>
              <small>
                Content #{post.content_item_id} · revision {post.content_revision}
              </small>
              <div className="assistant-refs">
                <button
                  className="link-button"
                  onClick={() => onOpenContent?.(post.content_item_id)}
                >
                  Открыть в редакторе
                </button>
              </div>
            </article>
          ))}
        </div>
        <SeriesApprovalSection
          key={run.id}
          runId={run.id}
          result={result}
          approval={latestSeriesApproval(seriesApprovals, run.id)}
          associationReady={seriesApprovalsReady}
          proposalBusy={busyKeys.has(`series-proposal:${run.id}`)}
          reviewBusy={
            latestSeriesApproval(seriesApprovals, run.id)
              ? busyKeys.has(
                `series-approval:${latestSeriesApproval(seriesApprovals, run.id)!.id}`,
              )
              : false
          }
          onOpenPlanner={onOpenPlanner}
          onCreateProposal={onCreateSeriesProposal}
          onApprove={onApproveSeries}
          onReject={onRejectSeries}
        />
      </div>
    );
  }

  if (result.scenario === 'drafts_tomorrow') {
    return (
      <div className="assistant-brief">
        <div className="assistant-summary">
          <small>{result.timezone} · редакционный день {result.target_local_date}</small>
          <p>
            Создано {result.draft_count} обычных черновика Content domain.
            Расписание и публикация не создаются до явного подтверждения отдельного предложения.
          </p>
          {result.editorial_context && (
            <small>
              Bounded context: {result.editorial_context.recent_count} recent ·{' '}
              {result.editorial_context.scheduled_count} scheduled refs
            </small>
          )}
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
                associationReady={draftApprovalsReady}
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

function operatorInputLabel(skill: AssistantSkillView): string {
  const properties = skill.operator_input_schema.properties;
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) {
    return 'Операторский ввод: схема недоступна';
  }
  const count = Object.keys(properties as Record<string, unknown>).length;
  return count === 0 ? 'Операторский ввод: не требуется' : `Операторских полей: ${count}`;
}

function executionLimitLabel(skill: AssistantSkillView): string {
  const limits = skill.execution_limits;
  const itemLimit = limits.max_items_per_tool
    ? ` · items/tool ≤ ${limits.max_items_per_tool}`
    : '';
  return [
    `steps ≤ ${limits.max_steps}`,
    `tools ≤ ${limits.max_tool_calls}`,
    `LLM ≤ ${limits.max_llm_calls}`,
    `time ≤ ${limits.max_seconds}s${itemLimit}`,
  ].join(' · ');
}

export function AssistantSkillCatalog({
  skills,
  loading,
  errorMessage,
  runningScenario,
  onRun,
}: {
  skills: AssistantSkillView[];
  loading: boolean;
  errorMessage: string | null;
  runningScenario: AssistantScenario | null;
  onRun?: (
    scenario: AssistantScenario,
    operatorInput?: AssistantContentSeriesOperatorInput,
  ) => void;
}) {
  const [seriesBrief, setSeriesBrief] = useState('');
  const [seriesPostCount, setSeriesPostCount] = useState(3);
  const normalizedSeriesBrief = seriesBrief.trim();
  const seriesInputValid = (
    normalizedSeriesBrief.length >= 20
    && normalizedSeriesBrief.length <= 2000
    && seriesPostCount >= 2
    && seriesPostCount <= 8
  );
  const failed = Boolean(errorMessage);
  const empty = !loading && !failed && skills.length === 0;

  return (
    <section className="assistant-history-card" aria-label="Каталог навыков Assistant">
      <div className="panel-heading">
        <div>
          <h2>Skill catalog</h2>
          <small>Только code-defined current skills; launcher не принимает skill id/version.</small>
        </div>
      </div>
      <AsyncRegion
        className="assistant-brief"
        loading={loading}
        error={failed}
        empty={empty}
        loadingLabel="Загружаю каталог навыков…"
        loadingFallback={
          <>
            <SkeletonBlock height={12} width="52%" />
            <SkeletonBlock height={72} />
          </>
        }
        emptyFallback={<div className="assistant-empty">Каталог навыков пуст.</div>}
        errorFallback={(
          <div className="assistant-empty">
            Каталог навыков не загружен. {errorMessage || 'Повторите позже.'}
          </div>
        )}
      >
        <div className="assistant-items">
          {skills.map((skill) => (
            <article className="assistant-item" key={`${skill.skill_id}@${skill.version}`}>
              <div className="assistant-item-heading">
                <div>
                  <strong>{skill.display_title}</strong>
                  <div>
                    <small>{skill.skill_id}@{skill.version} · {skill.category}</small>
                  </div>
                </div>
                <span className="assistant-run-status assistant-run-status-completed">
                  {skill.capability_classes.includes('draft_write') ? 'draft-write' : 'read-only'}
                </span>
              </div>
              <p>{skill.description}</p>
              <small>{skill.capability_summary}</small>
              <dl className="assistant-approval-facts">
                <div>
                  <dt>Context</dt>
                  <dd>{skill.context_requirements}</dd>
                </div>
                <div>
                  <dt>Resume</dt>
                  <dd>{skill.resumable ? skill.resume_policy : 'не поддерживается'}</dd>
                </div>
                <div>
                  <dt>Approval</dt>
                  <dd>
                    {skill.approval_requirement === 'none'
                      ? 'для запуска не требуется'
                      : skill.approval_requirement}
                  </dd>
                </div>
                <div>
                  <dt>Limits</dt>
                  <dd>{executionLimitLabel(skill)}</dd>
                </div>
                <div>
                  <dt>Input</dt>
                  <dd>{operatorInputLabel(skill)}</dd>
                </div>
              </dl>
              {skill.scenario === 'prepare_content_series' ? (
                <div className="assistant-series-form">
                  <label htmlFor="assistant-series-brief">Brief серии</label>
                  <textarea
                    id="assistant-series-brief"
                    value={seriesBrief}
                    minLength={20}
                    maxLength={2000}
                    required
                    aria-describedby="assistant-series-brief-help"
                    onChange={(event) => setSeriesBrief(event.target.value)}
                    disabled={runningScenario !== null}
                    rows={5}
                  />
                  <small id="assistant-series-brief-help">
                    20–2000 символов после trim. Backend validation остаётся authoritative.
                  </small>
                  <label htmlFor="assistant-series-count">Количество постов</label>
                  <select
                    id="assistant-series-count"
                    value={seriesPostCount}
                    onChange={(event) => setSeriesPostCount(Number(event.target.value))}
                    disabled={runningScenario !== null}
                  >
                    {[2, 3, 4, 5, 6, 7, 8].map((count) => (
                      <option value={count} key={count}>{count}</option>
                    ))}
                  </select>
                  <div className="assistant-refs">
                    <span>result: {skill.result_kind}</span>
                    <button
                      className="button secondary"
                      disabled={runningScenario !== null || !onRun || !seriesInputValid}
                      onClick={() => onRun?.(
                        skill.scenario,
                        { brief: normalizedSeriesBrief, post_count: seriesPostCount },
                      )}
                    >
                      {runningScenario === skill.scenario ? 'Запускаю…' : 'Подготовить серию'}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="assistant-refs">
                  <span>result: {skill.result_kind}</span>
                  <button
                    className="button secondary"
                    disabled={runningScenario !== null || !onRun}
                    onClick={() => onRun?.(skill.scenario)}
                  >
                    {runningScenario === skill.scenario ? 'Запускаю…' : 'Запустить'}
                  </button>
                </div>
              )}
            </article>
          ))}
        </div>
      </AsyncRegion>
    </section>
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
  const [skills, setSkills] = useState<AssistantSkillView[]>([]);
  const [catalogLoadState, setCatalogLoadState] = useState<ChannelLoadState | null>(null);
  const [catalogError, setCatalogError] = useState<{ channelId: number; message: string } | null>(null);
  const [runs, setRuns] = useState<AssistantRunView[]>([]);
  const [approvals, setApprovals] = useState<AssistantApprovalView[]>([]);
  const [approvalLoadState, setApprovalLoadState] = useState<ScopedLoadState | null>(null);
  const [approvalError, setApprovalError] = useState<{
    channelId: number;
    scopeKey: string;
    message: string;
  } | null>(null);
  const [seriesApprovals, setSeriesApprovals] = useState<AssistantSeriesApprovalView[]>([]);
  const [seriesApprovalLoadState, setSeriesApprovalLoadState] = useState<ScopedLoadState | null>(null);
  const [seriesApprovalError, setSeriesApprovalError] = useState<{
    channelId: number;
    scopeKey: string;
    message: string;
  } | null>(null);
  const [currentRun, setCurrentRun] = useState<AssistantRunView | null>(null);
  const [loadState, setLoadState] = useState<ChannelLoadState | null>(null);
  const [error, setError] = useState<{ channelId: number; message: string } | null>(null);
  const [runningScenario, setRunningScenario] = useState<AssistantScenario | null>(null);
  const [resumeRunId, setResumeRunId] = useState<number | null>(null);
  const [busyApprovalKeys, setBusyApprovalKeys] = useState<Set<string>>(() => new Set());
  const catalogOwnershipRef = useRef(new ChannelRequestOwnership());
  const requestOwnershipRef = useRef(new ChannelRequestOwnership());
  const approvalOwnershipRef = useRef(new ChannelRequestOwnership());
  const seriesApprovalOwnershipRef = useRef(new ChannelRequestOwnership());
  const runOwnershipRef = useRef(new ChannelRequestOwnership());
  const operationContextOwnershipRef = useRef(new ChannelRequestOwnership());
  const operationContextRef = useRef<{
    channelId: number | null;
    token: ChannelRequestToken | null;
  }>({ channelId: null, token: null });
  const operationLockRef = useRef(new ExclusiveOperationLock());
  const resumeLockRef = useRef(new ExclusiveOperationLock());
  const approvalLocksRef = useRef(new Map<string, ExclusiveOperationLock>());
  const validCatalogChannelRef = useRef<number | null>(null);
  const validDataChannelRef = useRef<number | null>(null);
  const channelIdRef = useRef<number | null>(channel?.id ?? null);
  const currentRunIdRef = useRef<number | null>(currentRun?.id ?? null);
  const currentChannelId = channel?.id ?? null;
  if (operationContextRef.current.channelId !== currentChannelId) {
    operationContextRef.current.channelId = currentChannelId;
    if (currentChannelId === null) {
      operationContextOwnershipRef.current.invalidate();
      operationContextRef.current.token = null;
    } else {
      operationContextRef.current.token = operationContextOwnershipRef.current.begin(currentChannelId);
    }
  }
  channelIdRef.current = currentChannelId;
  currentRunIdRef.current = currentRun?.id ?? null;
  const approvalBusyPrefix = currentChannelId === null ? null : `${currentChannelId}:`;
  const currentApprovalBusyKeys = new Set(
    approvalBusyPrefix === null
      ? []
      : Array.from(busyApprovalKeys)
        .filter((key) => key.startsWith(approvalBusyPrefix))
        .map((key) => key.slice(approvalBusyPrefix.length)),
  );

  const loadCatalog = useCallback(async () => {
    const channelId = channelIdRef.current;
    if (channelId === null) {
      catalogOwnershipRef.current.invalidate();
      validCatalogChannelRef.current = null;
      setSkills([]);
      setCatalogLoadState(null);
      setCatalogError(null);
      return;
    }
    const token = catalogOwnershipRef.current.begin(channelId);
    const hasValidData = validCatalogChannelRef.current === channelId;
    const isCurrent = () => catalogOwnershipRef.current.isCurrent(token, channelIdRef.current);
    if (!hasValidData) {
      validCatalogChannelRef.current = null;
      setSkills([]);
      setCatalogLoadState({ channelId, phase: 'loading' });
    }
    setCatalogError(null);
    try {
      const rows = await studioApi.assistantSkills(channelId);
      if (!isCurrent()) return;
      validCatalogChannelRef.current = channelId;
      setSkills(rows);
      setCatalogLoadState({ channelId, phase: 'loaded' });
    } catch (reason) {
      if (!isCurrent()) return;
      if (validCatalogChannelRef.current !== channelId) {
        setSkills([]);
        setCatalogLoadState({ channelId, phase: 'error-without-valid-data' });
      }
      setCatalogError({ channelId, message: errorMessage(reason) });
    }
  }, [channel?.id]);

  const loadHistory = useCallback(async () => {
    const channelId = channelIdRef.current;
    if (channelId === null) {
      requestOwnershipRef.current.invalidate();
      validDataChannelRef.current = null;
      setRuns([]);
      setApprovals([]);
      setSeriesApprovals([]);
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
      setSeriesApprovals([]);
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
        setApprovals([]);
        setSeriesApprovals([]);
        setCurrentRun(null);
        setLoadState({ channelId, phase: 'error-without-valid-data' });
      }
      setError({ channelId, message: errorMessage(reason) });
    }
  }, [channel?.id]);

  const loadDraftApprovals = useCallback(async (run: AssistantRunView | null) => {
    const channelId = channelIdRef.current;
    if (
      channelId === null
      || run === null
      || run.channel_id !== channelId
      || run.scenario !== 'drafts_tomorrow'
    ) {
      approvalOwnershipRef.current.invalidate();
      setApprovals([]);
      setApprovalLoadState(null);
      setApprovalError(null);
      return;
    }

    const scopeKey = String(run.id);
    const token = approvalOwnershipRef.current.begin(channelId, scopeKey);
    const isCurrent = () => approvalOwnershipRef.current.isCurrent(
      token,
      channelIdRef.current,
      currentRunIdRef.current === null ? null : String(currentRunIdRef.current),
    );
    setApprovals([]);
    setApprovalLoadState({ channelId, scopeKey, phase: 'loading' });
    setApprovalError(null);
    try {
      const rows = await studioApi.assistantRunApprovals(channelId, run.id);
      if (!isCurrent()) return;
      setApprovals(rows);
      setApprovalLoadState({ channelId, scopeKey, phase: 'loaded' });
    } catch (reason) {
      if (!isCurrent()) return;
      setApprovals([]);
      setApprovalLoadState({ channelId, scopeKey, phase: 'error-without-valid-data' });
      setApprovalError({ channelId, scopeKey, message: errorMessage(reason) });
    }
  }, []);

  const loadSeriesApprovals = useCallback(async (run: AssistantRunView | null) => {
    const channelId = channelIdRef.current;
    if (
      channelId === null
      || run === null
      || run.channel_id !== channelId
      || run.scenario !== 'prepare_content_series'
    ) {
      seriesApprovalOwnershipRef.current.invalidate();
      setSeriesApprovals([]);
      setSeriesApprovalLoadState(null);
      setSeriesApprovalError(null);
      return;
    }

    const scopeKey = String(run.id);
    const token = seriesApprovalOwnershipRef.current.begin(channelId, scopeKey);
    const isCurrent = () => seriesApprovalOwnershipRef.current.isCurrent(
      token,
      channelIdRef.current,
      currentRunIdRef.current === null ? null : String(currentRunIdRef.current),
    );
    setSeriesApprovals([]);
    setSeriesApprovalLoadState({ channelId, scopeKey, phase: 'loading' });
    setSeriesApprovalError(null);
    try {
      const rows = await studioApi.assistantRunSeriesApprovals(channelId, run.id);
      if (!isCurrent()) return;
      setSeriesApprovals(rows);
      setSeriesApprovalLoadState({ channelId, scopeKey, phase: 'loaded' });
    } catch (reason) {
      if (!isCurrent()) return;
      setSeriesApprovals([]);
      setSeriesApprovalLoadState({ channelId, scopeKey, phase: 'error-without-valid-data' });
      setSeriesApprovalError({ channelId, scopeKey, message: errorMessage(reason) });
    }
  }, []);

  useEffect(() => {
    catalogOwnershipRef.current.invalidate();
    approvalOwnershipRef.current.invalidate();
    seriesApprovalOwnershipRef.current.invalidate();
    runOwnershipRef.current.invalidate();
    setApprovals([]);
    setApprovalLoadState(null);
    setApprovalError(null);
    setSeriesApprovals([]);
    setSeriesApprovalLoadState(null);
    setSeriesApprovalError(null);
    setRunningScenario(null);
    setResumeRunId(null);
    void loadCatalog();
    void loadHistory();
  }, [loadCatalog, loadHistory]);

  useEffect(() => {
    void loadDraftApprovals(currentRun);
    void loadSeriesApprovals(currentRun);
  }, [
    currentRun?.id,
    currentRun?.channel_id,
    loadDraftApprovals,
    loadSeriesApprovals,
  ]);

  const runScenario = useCallback(async (
    scenario: AssistantScenario,
    operatorInput?: AssistantContentSeriesOperatorInput,
  ) => {
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
          const requestId = scenario === 'attention_today'
            ? null
            : newRequestId(scenario === 'prepare_content_series' ? 'series' : 'draft');
          const run = await studioApi.createAssistantRun(
            channelId,
            scenario,
            requestId,
            operatorInput ?? null,
          );
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

  const resumeRun = useCallback(async (run: AssistantRunView) => {
    const channelId = channelIdRef.current;
    if (channelId === null || !run.resumable) return;
    const result = await runExclusiveOperation(
      resumeLockRef.current,
      `resume:${run.id}`,
      async () => {
        const token = runOwnershipRef.current.begin(channelId);
        const isCurrent = () => (
          runOwnershipRef.current.isCurrent(token, channelIdRef.current)
          && currentRunIdRef.current === run.id
        );
        setResumeRunId(run.id);
        setError(null);
        try {
          const resumed = await studioApi.resumeAssistantRun(channelId, run.id);
          if (!isCurrent()) return;
          setCurrentRun(resumed);
          setRuns((previous) => mergeAssistantRun(previous, resumed));
          validDataChannelRef.current = channelId;
          setLoadState({ channelId, phase: 'loaded' });
        } catch (reason) {
          if (isCurrent()) {
            setError({ channelId, message: errorMessage(reason) });
          }
        } finally {
          if (isCurrent()) setResumeRunId(null);
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
    const runId = currentRunIdRef.current;
    const operationContextToken = operationContextRef.current.token;
    if (channelId === null || runId === null || operationContextToken === null) return;
    const isCurrent = () => (
      operationContextOwnershipRef.current.isCurrent(
        operationContextToken,
        channelIdRef.current,
      )
      && currentRunIdRef.current === runId
    );
    const scopedKey = `${channelId}:${key}`;
    let lock = approvalLocksRef.current.get(scopedKey);
    if (!lock) {
      lock = new ExclusiveOperationLock();
      approvalLocksRef.current.set(scopedKey, lock);
    }
    const result = await runExclusiveOperation(lock, scopedKey, async () => {
      setBusyApprovalKeys((previous) => new Set(previous).add(scopedKey));
      setError(null);
      try {
        const approval = await operation(channelId);
        if (!isCurrent()) return approval;
        setApprovals((previous) => mergeAssistantApproval(previous, approval));
        return approval;
      } catch (reason) {
        if (isCurrent()) {
          setError({ channelId, message: errorMessage(reason) });
        }
        return null;
      } finally {
        setBusyApprovalKeys((previous) => {
          const next = new Set(previous);
          next.delete(scopedKey);
          return next;
        });
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


  const runSeriesApprovalOperation = useCallback(async (
    key: string,
    operation: (channelId: number) => Promise<AssistantSeriesApprovalView>,
  ) => {
    const channelId = channelIdRef.current;
    const runId = currentRunIdRef.current;
    const operationContextToken = operationContextRef.current.token;
    if (channelId === null || runId === null || operationContextToken === null) return;
    const isCurrent = () => (
      operationContextOwnershipRef.current.isCurrent(
        operationContextToken,
        channelIdRef.current,
      )
      && currentRunIdRef.current === runId
    );
    const scopedKey = `${channelId}:${key}`;
    let lock = approvalLocksRef.current.get(scopedKey);
    if (!lock) {
      lock = new ExclusiveOperationLock();
      approvalLocksRef.current.set(scopedKey, lock);
    }
    const result = await runExclusiveOperation(lock, scopedKey, async () => {
      setBusyApprovalKeys((previous) => new Set(previous).add(scopedKey));
      setError(null);
      try {
        const approval = await operation(channelId);
        if (!isCurrent()) return approval;
        setSeriesApprovals((previous) => mergeAssistantSeriesApproval(previous, approval));
        return approval;
      } catch (reason) {
        if (isCurrent()) {
          setError({ channelId, message: errorMessage(reason) });
        }
        return null;
      } finally {
        setBusyApprovalKeys((previous) => {
          const next = new Set(previous);
          next.delete(scopedKey);
          return next;
        });
      }
    });
    if (!result.started) return;
  }, []);

  const createSeriesProposal = useCallback(async (
    sourceRunId: number,
    slots: AssistantSeriesApprovalSlotInput[],
  ) => {
    await runSeriesApprovalOperation(`series-proposal:${sourceRunId}`, (channelId) =>
      studioApi.createAssistantSeriesApproval(
        channelId,
        sourceRunId,
        slots,
        newRequestId('series-approval'),
      ));
  }, [runSeriesApprovalOperation]);

  const approveSeries = useCallback(async (batchId: number) => {
    await runSeriesApprovalOperation(`series-approval:${batchId}`, (channelId) =>
      studioApi.approveAssistantSeriesApproval(channelId, batchId));
  }, [runSeriesApprovalOperation]);

  const rejectSeries = useCallback(async (batchId: number) => {
    await runSeriesApprovalOperation(`series-approval:${batchId}`, (channelId) =>
      studioApi.rejectAssistantSeriesApproval(channelId, batchId));
  }, [runSeriesApprovalOperation]);

  if (!channel) {
    return <div className="sources-empty-page">Выберите канал, чтобы открыть Assistant.</div>;
  }

  const catalogView = resolveChannelDataView(catalogLoadState, channel.id, skills.length);
  const catalogLoading = catalogView === 'loading';
  const catalogFailed = catalogView === 'error-without-valid-data';
  const dataView = resolveChannelDataView(loadState, channel.id, runs.length);
  const initialLoading = dataView === 'loading';
  const loadFailed = dataView === 'error-without-valid-data';
  const loadedEmpty = dataView === 'loaded-empty';
  const draftApprovalScopeKey = currentRun?.scenario === 'drafts_tomorrow'
    ? String(currentRun.id)
    : null;
  const draftApprovalView = draftApprovalScopeKey === null
    ? null
    : resolveScopedDataView(
      approvalLoadState,
      channel.id,
      draftApprovalScopeKey,
      approvals.length,
    );
  const draftApprovalsReady = draftApprovalView === null
    || draftApprovalView === 'loaded-empty'
    || draftApprovalView === 'loaded-data';
  const draftApprovalsLoading = draftApprovalView === 'loading';
  const draftApprovalsFailed = draftApprovalView === 'error-without-valid-data';
  const currentApprovalError = (
    draftApprovalScopeKey !== null
    && approvalError?.channelId === channel.id
    && approvalError.scopeKey === draftApprovalScopeKey
  )
    ? approvalError.message
    : null;
  const seriesApprovalScopeKey = currentRun?.scenario === 'prepare_content_series'
    ? String(currentRun.id)
    : null;
  const seriesApprovalView = seriesApprovalScopeKey === null
    ? null
    : resolveScopedDataView(
      seriesApprovalLoadState,
      channel.id,
      seriesApprovalScopeKey,
      seriesApprovals.length,
    );
  const seriesApprovalsReady = seriesApprovalView === null
    || seriesApprovalView === 'loaded-empty'
    || seriesApprovalView === 'loaded-data';
  const seriesApprovalsLoading = seriesApprovalView === 'loading';
  const seriesApprovalsFailed = seriesApprovalView === 'error-without-valid-data';
  const currentSeriesApprovalError = (
    seriesApprovalScopeKey !== null
    && seriesApprovalError?.channelId === channel.id
    && seriesApprovalError.scopeKey === seriesApprovalScopeKey
  )
    ? seriesApprovalError.message
    : null;
  const running = runningScenario !== null;

  return (
    <div className="assistant-page">
      <header className="sources-topbar">
        <div>
          <small className="eyebrow">{channel.title || channel.tg_chat_id}</small>
          <h1>Assistant</h1>
          <p>
            Bounded work panel: attention flow читает состояние, draft/series flows создают
            обычные черновики, а постановка в план выполняется только через явное approval.
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
          {runningScenario === 'prepare_content_series' && (
            <InlineStatus>Готовлю series plan и bounded набор черновиков…</InlineStatus>
          )}
        </div>
      </header>

      {error?.channelId === channel.id && (
        <div className="banner error" role="alert">
          {error.message}
          <button aria-label="Закрыть ошибку" onClick={() => setError(null)}>×</button>
        </div>
      )}

      <AssistantSkillCatalog
        skills={skills}
        loading={catalogLoading}
        errorMessage={
          catalogFailed && catalogError?.channelId === channel.id
            ? catalogError.message
            : null
        }
        runningScenario={runningScenario}
        onRun={(scenario, operatorInput) => void runScenario(scenario, operatorInput)}
      />

      <AssistantAutomations channel={channel} skills={skills} />

      <section className="assistant-work-card">
        <div className="panel-heading">
          <div>
            <h2>Assistant result</h2>
            <small>
              {currentRun
                ? `run #${currentRun.id} · ${scenarioLabel(currentRun.scenario)} · ${currentRun.status}`
                : 'Запустите один из bounded сценариев'}
            </small>
            {currentRun?.skill_id && (
              <small>
                skill {currentRun.skill_id}@{currentRun.skill_version || '—'} · phase{' '}
                {currentRun.workflow_phase || 'legacy'}
              </small>
            )}
          </div>
          {currentRun?.resumable && (
            <button
              className="button secondary"
              disabled={resumeRunId === currentRun.id}
              onClick={() => void resumeRun(currentRun)}
            >
              {resumeRunId === currentRun.id ? 'Продолжаю…' : 'Продолжить'}
            </button>
          )}
        </div>
        {draftApprovalsLoading && (
          <InlineStatus>Проверяю статус предложений для этого запуска…</InlineStatus>
        )}
        {draftApprovalsFailed && currentRun && (
          <div className="assistant-empty" role="alert">
            Статус предложений не загружен. {currentApprovalError || 'Повторите загрузку.'}{' '}
            <button
              className="link-button"
              onClick={() => void loadDraftApprovals(currentRun)}
            >
              Повторить загрузку
            </button>
          </div>
        )}
        {seriesApprovalsLoading && (
          <InlineStatus>Проверяю статус предложения серии для этого запуска…</InlineStatus>
        )}
        {seriesApprovalsFailed && currentRun && (
          <div className="assistant-empty" role="alert">
            Статус предложения серии не загружен.{' '}
            {currentSeriesApprovalError || 'Повторите загрузку.'}{' '}
            <button
              className="link-button"
              onClick={() => void loadSeriesApprovals(currentRun)}
            >
              Повторить загрузку
            </button>
          </div>
        )}
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
            draftApprovalsReady={draftApprovalsReady}
            seriesApprovals={seriesApprovals}
            seriesApprovalsReady={seriesApprovalsReady}
            busyKeys={currentApprovalBusyKeys}
            onOpenPlanner={onOpenPlanner}
            onOpenContent={onOpenContent}
            onCreateProposal={createProposal}
            onApprove={approve}
            onReject={reject}
            onCreateSeriesProposal={createSeriesProposal}
            onApproveSeries={approveSeries}
            onRejectSeries={rejectSeries}
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
                <small>
                  {dateLabel(run.finished_at || run.created_at)}
                  {run.skill_id ? ` · ${run.skill_id}@${run.skill_version || '—'}` : ' · legacy'}
                  {run.resumable ? ' · можно продолжить' : ''}
                </small>
              </span>
              <span className={`assistant-run-status assistant-run-status-${run.status}`}>{run.status}</span>
            </button>
          ))}
        </AsyncRegion>
      </section>
    </div>
  );
}
