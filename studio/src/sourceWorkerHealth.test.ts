import { describe, expect, it } from 'vitest';

import type { SourceWorkerHealthView, SourceWorkerTickView } from './api';
import {
  recentSourceWorkerTicks,
  sourceWorkerTickErrors,
  sourceWorkerTotalErrors,
} from './sourceWorkerHealth';

function tick(index: number): SourceWorkerTickView {
  return {
    started_at: `2026-08-09T15:0${index}:00Z`,
    finished_at: `2026-08-09T15:0${index}:01Z`,
    window_selected: 10,
    scheduled: 8,
    processed: index,
    skipped_backoff: 1,
    skipped_busy: 1,
    lease_errors: index === 1 ? 1 : 0,
    failures: index === 2 ? 2 : 0,
    timeouts: index === 3 ? 1 : 0,
    ingestion_errors: index === 4 ? 1 : 0,
    unexpected_errors: index === 5 ? 1 : 0,
    new_documents: index,
    candidates_created: index,
    backlog_remaining: 0,
    stopped_early: false,
    duration_ms: 100 + index,
  };
}

function health(history: SourceWorkerTickView[]): SourceWorkerHealthView {
  return {
    running: true,
    started_at: '2026-08-09T15:00:00Z',
    ticks: history.length,
    history_size: 20,
    last_tick: history.length > 0 ? history[history.length - 1] : null,
    history,
    totals: {
      window_selected: 50,
      scheduled: 40,
      processed: 30,
      skipped_backoff: 2,
      skipped_busy: 3,
      lease_errors: 1,
      failures: 2,
      timeouts: 3,
      ingestion_errors: 4,
      unexpected_errors: 5,
      new_documents: 20,
      candidates_created: 18,
      backlog_remaining: 6,
    },
  };
}

describe('source worker health view helpers', () => {
  it('aggregates operational error categories without source-specific fields', () => {
    expect(sourceWorkerTickErrors(tick(2))).toBe(2);
    expect(sourceWorkerTotalErrors(health([tick(1)]))).toBe(15);
  });

  it('shows bounded recent history newest first without mutating server order', () => {
    const original = [tick(1), tick(2), tick(3), tick(4), tick(5)];
    const view = health(original);

    expect(recentSourceWorkerTicks(view, 3).map((item) => item.processed)).toEqual([5, 4, 3]);
    expect(original.map((item) => item.processed)).toEqual([1, 2, 3, 4, 5]);
  });

  it('clamps invalid history limits to a useful bounded range', () => {
    const view = health([tick(1), tick(2), tick(3)]);
    expect(recentSourceWorkerTicks(view, 0)).toHaveLength(1);
    expect(recentSourceWorkerTicks(view, 999)).toHaveLength(3);
  });
});
