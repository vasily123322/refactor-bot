import { describe, expect, it } from 'vitest';

import { ChannelRequestOwnership } from './asyncControl';
import type { AssistantAutomationRunSummaryView } from './api';
import {
  AUTOMATION_HISTORY_PAGE_SIZE,
  automationHistoryCursor,
  mergeAutomationHistoryPage,
  splitAutomationHistoryPage,
} from './automationRunHistory';

function run(id: number, scheduledFor = '2026-09-19T09:30:00Z'): AssistantAutomationRunSummaryView {
  return {
    id,
    scheduled_for: scheduledFor,
    status: 'completed',
    workflow_phase: 'completed',
    resumable: false,
    resume_state: 'completed',
    tokens_used: 0,
    model: null,
    created_at: null,
    started_at: null,
    finished_at: null,
    result_kind: null,
    result_metadata: {},
  };
}

describe('automation run history paging', () => {
  it('uses one lookahead row for older history', () => {
    const rows = Array.from(
      { length: AUTOMATION_HISTORY_PAGE_SIZE + 1 },
      (_, index) => run(AUTOMATION_HISTORY_PAGE_SIZE + 1 - index),
    );
    const page = splitAutomationHistoryPage(rows);
    expect(page.items).toHaveLength(AUTOMATION_HISTORY_PAGE_SIZE);
    expect(page.hasMore).toBe(true);
  });

  it('uses the visible last run as the tuple cursor', () => {
    expect(automationHistoryCursor([run(5), run(4)])).toEqual({
      scheduledFor: '2026-09-19T09:30:00Z',
      id: 4,
    });
  });

  it('deduplicates overlapping pages', () => {
    expect(
      mergeAutomationHistoryPage(
        [run(5), run(4)],
        [run(4), run(3), run(2)],
      ).map((row) => row.id),
    ).toEqual([5, 4, 3, 2]);
  });

  it('rejects stale load-more after refresh, automation switch, or channel switch', () => {
    const ownership = new ChannelRequestOwnership();
    const older = ownership.begin(1, '10');
    const refresh = ownership.begin(1, '10');
    expect(ownership.isCurrent(older, 1, '10')).toBe(false);
    expect(ownership.isCurrent(refresh, 1, '10')).toBe(true);

    const nextAutomation = ownership.begin(1, '11');
    expect(ownership.isCurrent(refresh, 1, '10')).toBe(false);
    expect(ownership.isCurrent(nextAutomation, 1, '11')).toBe(true);

    const nextChannel = ownership.begin(2, '11');
    expect(ownership.isCurrent(nextAutomation, 2, '11')).toBe(false);
    expect(ownership.isCurrent(nextChannel, 2, '11')).toBe(true);
  });
});
