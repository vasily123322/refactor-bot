import { useCallback, useEffect, useMemo, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import type { Channel, PlannerEntry } from './types';

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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingValue, setEditingValue] = useState('');

  const weekStart = useMemo(() => startOfWeek(anchor), [anchor]);
  const weekEnd = useMemo(() => addDays(weekStart, 7), [weekStart]);
  const days = useMemo(
    () => Array.from({ length: 7 }, (_, index) => addDays(weekStart, index)),
    [weekStart],
  );

  const load = useCallback(async () => {
    if (!channel) {
      setEntries([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setEntries(await studioApi.planner(channel.id, weekStart, weekEnd));
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setLoading(false);
    }
  }, [channel, weekEnd, weekStart]);

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

  const beginMove = (entry: PlannerEntry) => {
    setEditingId(entry.schedule_id);
    setEditingValue(localInputValue(entry.scheduled_at));
  };

  const saveMove = async (entry: PlannerEntry) => {
    if (!channel || !editingValue) return;
    const next = new Date(editingValue);
    if (Number.isNaN(next.getTime())) {
      setError('Некорректная дата');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const updated = await studioApi.reschedule(channel.id, entry.schedule_id, next);
      setEntries((current) =>
        current.map((row) => (row.schedule_id === updated.schedule_id ? updated : row)),
      );
      setEditingId(null);
      // The move may leave the current week; reload to keep the week truthful.
      await load();
    } catch (reason) {
      setError(errorText(reason));
      setLoading(false);
    }
  };

  const cancel = async (entry: PlannerEntry) => {
    if (!channel) return;
    if (!window.confirm('Отменить эту запланированную публикацию?')) return;
    setLoading(true);
    setError(null);
    try {
      const updated = await studioApi.cancelSchedule(channel.id, entry.schedule_id);
      setEntries((current) =>
        current.map((row) => (row.schedule_id === updated.schedule_id ? updated : row)),
      );
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setLoading(false);
    }
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
          <button className="button secondary" onClick={() => setAnchor(addDays(anchor, -7))}>←</button>
          <button className="button secondary" onClick={() => setAnchor(new Date())}>Сегодня</button>
          <button className="button secondary" onClick={() => setAnchor(addDays(anchor, 7))}>→</button>
          <button className="button secondary" onClick={() => void load()} disabled={loading}>↻</button>
        </div>
      </header>

      {error && <div className="banner error">{error}<button onClick={() => setError(null)}>×</button></div>}
      {!channel && <div className="planner-empty">Выберите канал в левой панели.</div>}

      {channel && (
        <div className="planner-week" aria-busy={loading}>
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
                          {entry.repeat_rule.enabled === true && <small>↻ повтор</small>}
                          {entry.last_error && <small className="planner-error-text">{entry.last_error}</small>}
                        </button>

                        {editingId === entry.schedule_id ? (
                          <div className="planner-reschedule">
                            <input
                              type="datetime-local"
                              value={editingValue}
                              onChange={(event) => setEditingValue(event.target.value)}
                            />
                            <button onClick={() => void saveMove(entry)}>✓</button>
                            <button onClick={() => setEditingId(null)}>×</button>
                          </div>
                        ) : (
                          <div className="planner-card-actions">
                            {entry.result_link && (
                              <a href={entry.result_link} target="_blank" rel="noreferrer" title="Открыть пост">↗</a>
                            )}
                            {mutable && <button onClick={() => beginMove(entry)} title="Перенести">🕒</button>}
                            {mutable && <button onClick={() => void cancel(entry)} title="Отменить">×</button>}
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
      )}
      {loading && <div className="planner-loading">Обновляю план…</div>}
    </section>
  );
}
