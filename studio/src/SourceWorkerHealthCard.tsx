import { useCallback, useEffect, useState } from 'react';

import {
  StudioApiError,
  studioApi,
  type SourceWorkerHealthView,
} from './api';
import {
  recentSourceWorkerTicks,
  sourceWorkerTickErrors,
  sourceWorkerTotalErrors,
} from './sourceWorkerHealth';

function errorMessage(error: unknown): string {
  if (error instanceof StudioApiError || error instanceof Error) return error.message;
  return 'Неизвестная ошибка';
}

function timeLabel(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('ru', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date);
}

export function SourceWorkerHealthCard() {
  const [health, setHealth] = useState<SourceWorkerHealthView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setHealth(await studioApi.sourceWorkerHealth());
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const last = health?.last_tick ?? null;
  const recent = health ? recentSourceWorkerTicks(health) : [];
  const totalErrors = health ? sourceWorkerTotalErrors(health) : 0;

  return (
    <section className="source-worker-health-card" aria-label="Source worker health">
      <div className="source-worker-health-head">
        <div>
          <div className="source-worker-title-row">
            <h2>Worker health</h2>
            <span className={health?.running ? 'worker-running' : 'worker-stopped'}>
              {health?.running ? 'running' : 'stopped'}
            </span>
          </div>
          <small>
            Process-local aggregate telemetry. Без source IDs, URL и error text.
          </small>
        </div>
        <button className="button secondary compact" onClick={() => void load()} disabled={loading}>
          {loading ? 'Обновляю…' : '↻ Health'}
        </button>
      </div>

      {error && <div className="source-worker-health-error">Health API: {error}</div>}

      {health && (
        <>
          <div className="source-worker-metrics">
            <div><small>ticks</small><strong>{health.ticks}</strong></div>
            <div><small>processed</small><strong>{health.totals.processed}</strong></div>
            <div><small>new docs</small><strong>{health.totals.new_documents}</strong></div>
            <div><small>candidates</small><strong>{health.totals.candidates_created}</strong></div>
            <div><small>backoff skips</small><strong>{health.totals.skipped_backoff}</strong></div>
            <div><small>busy skips</small><strong>{health.totals.skipped_busy}</strong></div>
            <div><small>errors</small><strong>{totalErrors}</strong></div>
          </div>

          <div className="source-worker-last-tick">
            <div className="panel-heading">
              <div>
                <h3>Последний tick</h3>
                <small>{last ? timeLabel(last.finished_at) : 'данных пока нет'}</small>
              </div>
            </div>
            {last ? (
              <div className="source-worker-tick-summary">
                <span>window {last.window_selected}</span>
                <span>scheduled {last.scheduled}</span>
                <span>processed {last.processed}</span>
                <span>docs +{last.new_documents}</span>
                <span>candidates +{last.candidates_created}</span>
                <span>backlog {last.backlog_remaining}</span>
                <span>errors {sourceWorkerTickErrors(last)}</span>
                <span>{last.duration_ms} ms</span>
                {last.stopped_early && <span>stopped early</span>}
              </div>
            ) : (
              <div className="empty-state source-worker-empty">Worker ещё не завершил ни одного tick.</div>
            )}
          </div>

          {recent.length > 0 && (
            <div className="source-worker-history-wrap">
              <div className="panel-heading">
                <div>
                  <h3>Последние ticks</h3>
                  <small>{recent.length} из {health.history.length} в ring buffer</small>
                </div>
              </div>
              <div className="source-worker-history" role="table" aria-label="Recent source worker ticks">
                <div className="source-worker-history-row source-worker-history-header" role="row">
                  <span>time</span>
                  <span>processed</span>
                  <span>docs</span>
                  <span>errors</span>
                  <span>backlog</span>
                  <span>duration</span>
                </div>
                {recent.map((tick) => (
                  <div
                    className="source-worker-history-row"
                    role="row"
                    key={`${tick.started_at}:${tick.finished_at}`}
                  >
                    <span>{timeLabel(tick.finished_at)}</span>
                    <span>{tick.processed}/{tick.scheduled}</span>
                    <span>+{tick.new_documents}</span>
                    <span>{sourceWorkerTickErrors(tick)}</span>
                    <span>{tick.backlog_remaining}</span>
                    <span>{tick.duration_ms} ms</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
