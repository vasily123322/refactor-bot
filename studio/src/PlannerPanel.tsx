import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import { AsyncRegion, InlineStatus, SkeletonBlock } from './AsyncUI';
import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveScopedDataView,
  runExclusiveOperation,
  type ScopedLoadState,
} from './asyncControl';
import { plannerAttemptLabel } from './plannerAttempts';
import { emitStudioHaptic } from './studioHaptics';
import type { Channel, PlannerEntry } from './types';
import './planner.css';

function startOfWeek(value: Date): Date {
  const next = new Date(value);
  next.setHours(0, 0, 0, 0);
  const mondayOffset = (next.getDay() + 6) % 7;
  next.setDate(next.getDate() - mondayOffset);
  return next;
}

function addDays(value: Date, days: number): Date {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}

function dayKey(value: Date): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}

function localInputValue(value: string): string {
  const date = new Date(value);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function errorText(error: unknown): string {
  if (error instanceof StudioApiError || error instanceof Error) return error.message;
  return 'Не удалось обновить планер';
}

function statusLabel(entry: PlannerEntry): string {
  const value = entry.publication_status || entry.schedule_status;
  const labels: Record<string, string> = {
    queued: 'В очереди',
    sending: 'Отправляется',
    published: 'Опубликован',
    failed: 'Ошибка',
    skipped: 'Пропущен',
    cancelled: 'Отменён',
    pending: 'Запланирован',
    completed: 'Готово',
  };
  return labels[value] || value;
}

export function PlannerPanel({
  channel,
  onOpenContent,
}: {
  channel: Channel | null;
  onOpenContent: (contentId: number) => void;
}) {
  const [anchor, setAnchor] = useState(() => new Date());
  const [entries, setEntries] = useState<PlannerEntry[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [loadState, setLoadState] = useState<ScopedLoadState | null>(null);
  const [busyKeys, setBusyKeys] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingValue, setEditingValue] = useState('');

  const weekStart = useMemo(() => startOfWeek(anchor), [anchor]);
  const weekEnd = useMemo(() => addDays(weekStart, 7), [weekStart]);
  const days = useMemo(
    () => Array.from({ length: 7 }, (_, index) => addDays(weekStart, index)),
    [weekStart],
  );
  const scopeKey = useMemo(
    () => `${channel?.id ?? 'none'}:${dayKey(weekStart)}`,
    [channel?.id, weekStart],
  );
  const requestOwnershipRef = useRef(new ChannelRequestOwnership());
  const validDataScopeRef = useRef<string | null>(null);
  const operationLocksRef = useRef(new Map<string, ExclusiveOperationLock>());
  const channelIdRef = useRef<number | null>(channel?.id ?? null);
  const scopeKeyRef = useRef(scopeKey);
  channelIdRef.current = channel?.id ?? null;
  scopeKeyRef.current = scopeKey;

  const load = useCallback(async (): Promise<boolean> => {
    const channelId = channel?.id ?? null;
    const requestScopeKey = scopeKey;
    if (channelId === null) {
      requestOwnershipRef.current.invalidate();
      validDataScopeRef.current = null;
      setEntries([]);
      setLoadState(null);
      setRefreshing(false);
      setError(null);
      return false;
    }

    const token = requestOwnershipRef.current.begin(channelId, requestScopeKey);
    const hasValidData = validDataScopeRef.current === requestScopeKey;
    const isCurrent = () => requestOwnershipRef.current.isCurrent(
      token,
      channelIdRef.current,
      scopeKeyRef.current,
    );

    setRefreshing(true);
    setError(null);
    if (!hasValidData) {
      validDataScopeRef.current = null;
      setEntries([]);
      setLoadState({ channelId, scopeKey: requestScopeKey, phase: 'loading' });
    }

    try {
      const rows = await studioApi.planner(channelId, weekStart, weekEnd);
      if (!isCurrent()) return false;
      validDataScopeRef.current = requestScopeKey;
      setEntries(rows);
      setLoadState({ channelId, scopeKey: requestScopeKey, phase: 'loaded' });
      return true;
    } catch (reason) {
      if (!isCurrent()) return false;
      if (validDataScopeRef.current !== requestScopeKey) {
        setEntries([]);
        setLoadState({
          channelId,
          scopeKey: requestScopeKey,
          phase: 'error-without-valid-data',
        });
      }
      setError(errorText(reason));
      return false;
    } finally {
      if (isCurrent()) setRefreshing(false);
    }
  }, [channel?.id, scopeKey, weekEnd, weekStart]);

  useEffect(() => {
    void load();
  }, [load]);

  const grouped = useMemo(() => {
    const map = new Map<string, PlannerEntry[]>();
    for (const entry of entries) {
      const key = dayKey(new Date(entry.scheduled_at));
      const list = map.get(key) ?? [];
      list.push(entry);
      map.set(key, list);
    }
    return map;
  }, [entries]);

  const plannerDataView = channel
    ? resolveScopedDataView(loadState, channel.id, scopeKey, entries.length)
    : null;
  const initialLoading = plannerDataView === 'loading';
  const loadFailedWithoutValidData = plannerDataView === 'error-without-valid-data';
  const hasValidData = plannerDataView === 'loaded-data' || plannerDataView === 'loaded-empty';

  const runScheduleOperation = useCallback(async (
    resourceKey: string,
    operationKey: string,
    operation: () => Promise<void>,
  ): Promise<boolean> => {
    let lock = operationLocksRef.current.get(resourceKey);
    if (!lock) {
      lock = new ExclusiveOperationLock();
      operationLocksRef.current.set(resourceKey, lock);
    }
    const result = await runExclusiveOperation(
      lock,
      operationKey,
      operation,
      (activeKey) => setBusyKeys((current) => {
        const next = new Set(current);
        if (activeKey) next.add(operationKey);
        else next.delete(operationKey);
        return next;
      }),
    );
    if (!lock.isLocked()) operationLocksRef.current.delete(resourceKey);
    return result.started;
  }, []);

  const beginMove = (entry: PlannerEntry) => {
    setEditingId(entry.schedule_id);
    setEditingValue(localInputValue(entry.scheduled_at));
  };

  const saveMove = async (entry: PlannerEntry) => {
    if (!channel || !editingValue) return;
    const next = new Date(editingValue);
    if (Number.isNaN(next.getTime())) {
      emitStudioHaptic('validation-error');
      setError('Некорректная дата');
      return;
    }

    const operationChannelId = channel.id;
    const operationScopeKey = scopeKey;
    const resourceKey = `schedule:${operationChannelId}:${entry.schedule_id}`;
    const operationKey = `reschedule:${operationChannelId}:${entry.schedule_id}`;
    await runScheduleOperation(resourceKey, operationKey, async () => {
      setError(null);
      try {
        const updated = await studioApi.reschedule(operationChannelId, entry.schedule_id, next);
        if (
          channelIdRef.current !== operationChannelId
          || scopeKeyRef.current !== operationScopeKey
        ) {
          return;
        }
        setEntries((current) =>
          current.map((row) => (row.schedule_id === updated.schedule_id ? updated : row)),
        );
        setEditingId(null);
        await load();
      } catch (reason) {
        if (
          channelIdRef.current === operationChannelId
          && scopeKeyRef.current === operationScopeKey
        ) {
          emitStudioHaptic('action-error');
          setError(errorText(reason));
        }
      }
    });
  };

  const cancel = async (entry: PlannerEntry) => {
    if (!channel) return;
    emitStudioHaptic('destructive-confirmation');
    if (!window.confirm('Отменить эту запланированную публикацию?')) return;

    const operationChannelId = channel.id;
    const operationScopeKey = scopeKey;
    const resourceKey = `schedule:${operationChannelId}:${entry.schedule_id}`;
    const operationKey = `cancel:${operationChannelId}:${entry.schedule_id}`;
    await runScheduleOperation(resourceKey, operationKey, async () => {
      setError(null);
      try {
        const updated = await studioApi.cancelSchedule(operationChannelId, entry.schedule_id);
        if (
          channelIdRef.current !== operationChannelId
          || scopeKeyRef.current !== operationScopeKey
        ) {
          return;
        }
        setEntries((current) =>
          current.map((row) => (row.schedule_id === updated.schedule_id ? updated : row)),
        );
      } catch (reason) {
        if (
          channelIdRef.current === operationChannelId
          && scopeKeyRef.current === operationScopeKey
        ) {
          emitStudioHaptic('action-error');
          setError(errorText(reason));
        }
      }
    });
  };

  return (
    <section className="planner-workspace">
      <header className="planner-header">
        <div>
          <small className="eyebrow">{channel?.title || 'Выберите канал'}</small>
          <h1>Контент-план</h1>
          <p>Schedule и реальный статус Telegram-публикации в одном представлении.</p>
        </div>
        <div className="planner-controls">
          <button className="button secondary" aria-label="Предыдущая неделя" onClick={() => setAnchor(addDays(anchor, -7))}>←</button>
          <button className="button secondary" onClick={() => setAnchor(new Date())}>Сегодня</button>
          <button className="button secondary" aria-label="Следующая неделя" onClick={() => setAnchor(addDays(anchor, 7))}>→</button>
          <button className="button secondary" aria-label="Обновить план" onClick={() => void load()} disabled={refreshing}>↻</button>
        </div>
      </header>

      {error && <div className="banner error" role="alert">{error}<button aria-label="Закрыть ошибку" onClick={() => setError(null)}>×</button></div>}
      {!channel && <div className="planner-empty">Выберите канал в левой панели.</div>}

      {channel && (
        <AsyncRegion
          loading={initialLoading}
          error={loadFailedWithoutValidData}
          empty={false}
          loadingLabel="Загружаю контент-план…"
          loadingFallback={
            <div className="planner-week" aria-hidden="true">
              {days.map((day) => (
                <div className="planner-day" key={dayKey(day)}>
                  <div className="planner-day-heading">
                    <SkeletonBlock height={12} width="62%" />
                  </div>
                  <div className="planner-cards">
                    <SkeletonBlock height={74} radius={10} />
                    <SkeletonBlock height={54} radius={10} />
                  </div>
                </div>
              ))}
            </div>
          }
          emptyFallback={null}
          errorFallback={<div className="planner-empty">План не загружен. Повторите попытку.</div>}
        >
          <div className="planner-week" aria-busy={refreshing || undefined}>
          {days.map((day) => {
            const rows = grouped.get(dayKey(day)) ?? [];
            const today = dayKey(day) === dayKey(new Date());
            return (
              <div className={today ? 'planner-day today' : 'planner-day'} key={dayKey(day)}>
                <div className="planner-day-heading">
                  <span>{new Intl.DateTimeFormat('ru', { weekday: 'short' }).format(day)}</span>
                  <strong>{day.getDate()}</strong>
                  <small>{new Intl.DateTimeFormat('ru', { month: 'short' }).format(day)}</small>
                </div>
                <div className="planner-cards">
                  {rows.length === 0 && <div className="planner-day-empty">Свободно</div>}
                  {rows.map((entry) => {
                    const mutable =
                      entry.schedule_status === 'pending' &&
                      (entry.publication_status === 'queued' || entry.publication_status === null);
                    const attemptLabel = plannerAttemptLabel(entry);
                    const rescheduleKey = `reschedule:${channel.id}:${entry.schedule_id}`;
                    const cancelKey = `cancel:${channel.id}:${entry.schedule_id}`;
                    const scheduleBusy = busyKeys.has(rescheduleKey) || busyKeys.has(cancelKey);
                    return (
                      <article className={`planner-card publication-${entry.publication_status || entry.schedule_status}`} key={entry.schedule_id}>
                        <button className="planner-card-main" onClick={() => onOpenContent(entry.content_item_id)}>
                          <time>
                            {new Intl.DateTimeFormat('ru', {
                              hour: '2-digit',
                              minute: '2-digit',
                            }).format(new Date(entry.scheduled_at))}
                          </time>
                          <strong>{entry.content_title || `Пост #${entry.content_item_id}`}</strong>
                          <span className="planner-card-status">{statusLabel(entry)}</span>
                          {attemptLabel && <small>{attemptLabel}</small>}
                          {entry.repeat_rule.enabled === true && <small>↻ повтор</small>}
                          {entry.last_error && <small className="planner-error-text">{entry.last_error}</small>}
                        </button>

                        {editingId === entry.schedule_id ? (
                          <div className="planner-reschedule">
                            <input
                              type="datetime-local"
                              aria-label="Новая дата и время публикации"
                              value={editingValue}
                              onChange={(event) => setEditingValue(event.target.value)}
                            />
                            <button
                              aria-label="Сохранить перенос"
                              disabled={scheduleBusy}
                              onClick={() => void saveMove(entry)}
                            >
                              {busyKeys.has(rescheduleKey) ? '…' : '✓'}
                            </button>
                            <button
                              aria-label="Закрыть перенос"
                              disabled={scheduleBusy}
                              onClick={() => setEditingId(null)}
                            >
                              ×
                            </button>
                          </div>
                        ) : (
                          <div className="planner-card-actions">
                            {entry.result_link && (
                              <a href={entry.result_link} target="_blank" rel="noreferrer" title="Открыть пост">↗</a>
                            )}
                            {mutable && (
                              <button
                                onClick={() => beginMove(entry)}
                                title="Перенести"
                                aria-label="Перенести публикацию"
                                disabled={scheduleBusy}
                              >
                                🕒
                              </button>
                            )}
                            {mutable && (
                              <button
                                onClick={() => void cancel(entry)}
                                title="Отменить"
                                aria-label="Отменить публикацию"
                                disabled={scheduleBusy}
                              >
                                {busyKeys.has(cancelKey) ? '…' : '×'}
                              </button>
                            )}
                          </div>
                        )}
                      </article>
                    );
                  })}
                </div>
              </div>
            );
          })}
          </div>
        </AsyncRegion>
      )}
      {refreshing && hasValidData && (
        <InlineStatus className="planner-loading">Обновляю план…</InlineStatus>
      )}
    </section>
  );
}
