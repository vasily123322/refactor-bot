import { useCallback, useEffect, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import type { AIActivityRunView, AIActivityView } from './api';
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
    second: '2-digit',
  }).format(date);
}

function usageLabel(used: number, limit: number | null): string {
  if (limit === null) return `${used.toLocaleString('ru-RU')} · без локального лимита`;
  const percent = limit > 0 ? Math.min(999, Math.round((used / limit) * 100)) : 0;
  return `${used.toLocaleString('ru-RU')} / ${limit.toLocaleString('ru-RU')} · ${percent}%`;
}

function countLabel(counts: Record<string, number>): string {
  const entries = Object.entries(counts).sort(([left], [right]) => left.localeCompare(right));
  if (entries.length === 0) return 'нет запусков';
  return entries.map(([status, count]) => `${status}: ${count}`).join(' · ');
}

function runTitle(run: AIActivityRunView): string {
  return run.kind === 'rewrite' ? 'Rewrite' : 'Enrichment';
}

export function AIStudioPanel({ channel }: { channel: Channel | null }) {
  const [activity, setActivity] = useState<AIActivityView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!channel) {
      setActivity(null);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setActivity(await studioApi.aiActivity(channel.id, 100));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }, [channel]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!channel) {
    return <div className="sources-empty-page">Выберите канал, чтобы открыть AI Studio.</div>;
  }

  const usage = activity?.usage;

  return (
    <div className="ai-studio-page">
      <header className="sources-topbar">
        <div>
          <small className="eyebrow">{channel.title || channel.tg_chat_id}</small>
          <h1>AI Studio</h1>
          <p>Read-only usage и provenance. Эта страница не запускает генерацию и не расходует AI-токены.</p>
        </div>
        <div className="top-actions">
          <button className="button secondary" onClick={() => void load()} disabled={busy}>
            {busy ? 'Обновляю…' : '↻ Обновить'}
          </button>
        </div>
      </header>

      {error && (
        <div className="banner error" role="alert">
          {error}
          <button onClick={() => setError(null)}>×</button>
        </div>
      )}

      <section className="ai-usage-grid">
        <article className="ai-stat-card">
          <small>Channel AI</small>
          <strong>{usage?.configured ? (usage.enabled ? 'Включён' : 'Выключен') : 'Не настроен'}</strong>
          <span>{usage?.model || '—'}</span>
        </article>
        <article className="ai-stat-card">
          <small>Сегодня</small>
          <strong>{usage ? usage.tokens_used_day.toLocaleString('ru-RU') : '—'}</strong>
          <span>{usage ? usageLabel(usage.tokens_used_day, usage.tokens_limit_day) : '—'}</span>
        </article>
        <article className="ai-stat-card">
          <small>Месяц</small>
          <strong>{usage ? usage.tokens_used_month.toLocaleString('ru-RU') : '—'}</strong>
          <span>{usage ? usageLabel(usage.tokens_used_month, usage.tokens_limit_month) : '—'}</span>
        </article>
        <article className="ai-stat-card">
          <small>Request settings</small>
          <strong>{usage?.max_tokens ? `${usage.max_tokens} max tokens` : '—'}</strong>
          <span>{usage?.temperature !== null && usage?.temperature !== undefined ? `temperature ${usage.temperature}` : '—'}</span>
        </article>
      </section>

      <section className="ai-status-grid">
        <article className="ai-status-card">
          <strong>Enrichment runs</strong>
          <span>{activity ? countLabel(activity.enrichment_counts) : '—'}</span>
        </article>
        <article className="ai-status-card">
          <strong>Rewrite runs</strong>
          <span>{activity ? countLabel(activity.rewrite_counts) : '—'}</span>
        </article>
      </section>

      <section className="ai-runs-card">
        <div className="panel-heading">
          <div>
            <h2>Последние AI runs</h2>
            <small>{activity?.runs.length || 0} записей · без source/generated content</small>
          </div>
        </div>
        <div className="ai-run-list">
          {!activity?.runs.length && <div className="empty-state">AI provenance пока пуст.</div>}
          {activity?.runs.map((run) => (
            <article className="ai-run-row" key={`${run.kind}:${run.id}`}>
              <div className="ai-run-main">
                <span className={`ai-run-kind ai-run-kind-${run.kind}`}>{runTitle(run)}</span>
                <strong>run #{run.id}</strong>
                <span>candidate #{run.candidate_id}</span>
              </div>
              <div className="ai-run-meta">
                <span className={`ai-run-status ai-run-status-${run.status}`}>{run.status}</span>
                <span>{run.provider}</span>
                <span>{run.model || 'no model'}</span>
                <span>{run.input_chars.toLocaleString('ru-RU')} input chars</span>
                <span>{dateLabel(run.finished_at || run.created_at || run.started_at)}</span>
                {run.error_type && <span className="ai-run-error">{run.error_type}</span>}
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
