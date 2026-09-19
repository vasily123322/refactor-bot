import { describe, expect, it } from 'vitest';

import { ChannelRequestOwnership } from './asyncControl';
import {
  INBOX_HISTORY_PAGE_SIZE,
  inboxHistoryCursor,
  mergeInboxHistoryPage,
  splitInboxHistoryPage,
} from './inboxCandidateHistory';
import { inboxStatusScope } from './inboxCandidateStatus';
import type { ContentCandidateView } from './types';

function item(id: number, publishedAt: string | null): ContentCandidateView {
  return {
    id,
    source_document_id: id,
    connector_id: 1,
    status: 'new',
    suggested_action: null,
    score: null,
    topic: null,
    summary: null,
    source_title: `Candidate ${id}`,
    source_url: null,
    excerpt: '',
    published_at: publishedAt,
    fetched_at: '2026-09-19T12:00:00Z',
    created_at: '2026-09-19T12:00:00Z',
    reuse_policy: 'summarize',
    suggested_post: null,
    channel_dm: null,
  };
}

describe('Inbox history paging', () => {
  it('uses one lookahead row to determine whether older candidates exist', () => {
    const rows = Array.from(
      { length: INBOX_HISTORY_PAGE_SIZE + 1 },
      (_, index) => item(INBOX_HISTORY_PAGE_SIZE + 1 - index, '2026-09-19T12:00:00Z'),
    );

    const page = splitInboxHistoryPage(rows);

    expect(page.items).toHaveLength(INBOX_HISTORY_PAGE_SIZE);
    expect(page.hasMore).toBe(true);
    expect(page.items.at(-1)?.id).toBe(2);
  });

  it('builds non-null and null-tail cursors from the visible last row', () => {
    expect(inboxHistoryCursor([
      item(8, '2026-09-19T12:00:00Z'),
      item(7, '2026-09-19T12:00:00Z'),
    ])).toEqual({
      publishedAt: '2026-09-19T12:00:00Z',
      id: 7,
    });

    expect(inboxHistoryCursor([
      item(5, null),
      item(4, null),
    ])).toEqual({ publishedAt: null, id: 4 });
  });

  it('deduplicates overlapping pages without reordering loaded candidates', () => {
    expect(
      mergeInboxHistoryPage(
        [item(5, null), item(4, null)],
        [item(4, null), item(3, null), item(2, null)],
      ).map((row) => row.id),
    ).toEqual([5, 4, 3, 2]);
  });

  it('rejects an older-page completion after status ownership changes', () => {
    const ownership = new ChannelRequestOwnership();
    const newScope = inboxStatusScope('new');
    const dismissedScope = inboxStatusScope('dismissed');
    const older = ownership.begin(1, newScope);
    ownership.begin(1, dismissedScope);

    expect(ownership.isCurrent(older, 1, dismissedScope)).toBe(false);
  });
});
