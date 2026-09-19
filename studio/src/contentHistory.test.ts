import { describe, expect, it } from 'vitest';

import { ChannelRequestOwnership } from './asyncControl';
import {
  CONTENT_HISTORY_PAGE_SIZE,
  contentHistoryCursor,
  mergeContentHistoryPage,
  splitContentHistoryPage,
} from './contentHistory';
import type { ContentSummary } from './types';

function item(id: number, updatedAt = '2026-09-19T12:00:00Z'): ContentSummary {
  return {
    id,
    channel_id: 1,
    kind: 'post',
    status: 'draft',
    title: `Item ${id}`,
    current_revision: 1,
    updated_at: updatedAt,
  };
}

describe('content history paging', () => {
  it('uses one lookahead row to determine whether older content exists', () => {
    const rows = Array.from(
      { length: CONTENT_HISTORY_PAGE_SIZE + 1 },
      (_, index) => item(CONTENT_HISTORY_PAGE_SIZE + 1 - index),
    );

    const page = splitContentHistoryPage(rows);

    expect(page.items).toHaveLength(CONTENT_HISTORY_PAGE_SIZE);
    expect(page.hasMore).toBe(true);
    expect(page.items.at(-1)?.id).toBe(2);
  });

  it('uses the visible last row as the deterministic tuple cursor', () => {
    const rows = [
      item(8, '2026-09-19T12:00:00Z'),
      item(7, '2026-09-19T12:00:00Z'),
    ];

    expect(contentHistoryCursor(rows)).toEqual({
      updatedAt: '2026-09-19T12:00:00Z',
      id: 7,
    });
  });

  it('deduplicates overlapping pages without reordering already loaded rows', () => {
    expect(
      mergeContentHistoryPage(
        [item(5), item(4)],
        [item(4), item(3), item(2)],
      ).map((row) => row.id),
    ).toEqual([5, 4, 3, 2]);
  });

  it('invalidates an older-page response when channel ownership changes', () => {
    const ownership = new ChannelRequestOwnership();
    const olderPage = ownership.begin(1, 'content-history:older');
    const nextChannel = ownership.begin(2, 'content-history:first');

    expect(
      ownership.isCurrent(olderPage, 2, 'content-history:older'),
    ).toBe(false);
    expect(
      ownership.isCurrent(nextChannel, 2, 'content-history:first'),
    ).toBe(true);
  });
});
