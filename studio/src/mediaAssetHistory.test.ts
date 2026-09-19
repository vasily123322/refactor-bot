import { describe, expect, it } from 'vitest';

import { ChannelRequestOwnership } from './asyncControl';
import type { MediaAssetView } from './api';
import {
  MEDIA_ASSET_HISTORY_PAGE_SIZE,
  mediaAssetHistoryCursor,
  mergeMediaAssetHistoryPage,
  prependMediaAsset,
  splitMediaAssetHistoryPage,
} from './mediaAssetHistory';

function asset(id: number, createdAt = '2026-09-19T13:00:00Z'): MediaAssetView {
  return {
    id,
    channel_id: 1,
    kind: 'photo',
    source: 'test',
    transport: 'telegram',
    label: `Asset ${id}`,
    mime_type: null,
    width: null,
    height: null,
    duration_seconds: null,
    size_bytes: null,
    created_at: createdAt,
  };
}

describe('media asset history paging', () => {
  it('uses one lookahead row to expose older assets', () => {
    const rows = Array.from(
      { length: MEDIA_ASSET_HISTORY_PAGE_SIZE + 1 },
      (_, index) => asset(MEDIA_ASSET_HISTORY_PAGE_SIZE + 1 - index),
    );
    const page = splitMediaAssetHistoryPage(rows);
    expect(page.items).toHaveLength(MEDIA_ASSET_HISTORY_PAGE_SIZE);
    expect(page.hasMore).toBe(true);
  });

  it('uses the visible last row as the tuple cursor', () => {
    expect(mediaAssetHistoryCursor([
      asset(8),
      asset(7),
    ])).toEqual({
      createdAt: '2026-09-19T13:00:00Z',
      id: 7,
    });
  });

  it('deduplicates overlapping older pages', () => {
    expect(
      mergeMediaAssetHistoryPage(
        [asset(5), asset(4)],
        [asset(4), asset(3), asset(2)],
      ).map((row) => row.id),
    ).toEqual([5, 4, 3, 2]);
  });

  it('prepends a newly created asset without duplication', () => {
    expect(
      prependMediaAsset([asset(5), asset(4)], asset(5, '2026-09-19T14:00:00Z'))
        .map((row) => row.id),
    ).toEqual([5, 4]);
  });

  it('rejects stale older-page ownership after refresh or channel change', () => {
    const ownership = new ChannelRequestOwnership();
    const older = ownership.begin(1, 'media-assets:older');
    const refresh = ownership.begin(1, 'media-assets:first');
    expect(ownership.isCurrent(older, 1, 'media-assets:older')).toBe(false);
    expect(ownership.isCurrent(refresh, 1, 'media-assets:first')).toBe(true);

    const nextChannel = ownership.begin(2, 'media-assets:first');
    expect(ownership.isCurrent(refresh, 2, 'media-assets:first')).toBe(false);
    expect(ownership.isCurrent(nextChannel, 2, 'media-assets:first')).toBe(true);
  });
});
