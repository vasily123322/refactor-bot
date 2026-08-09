import { describe, expect, it } from 'vitest';

import type { MediaAssetView } from './api';
import {
  mediaAssetBlockPatch,
  mediaAssetOptionLabel,
  mediaCollectionItem,
  mediaCollectionItems,
  moveMediaCollectionItem,
} from './richMediaAssets';

const asset: MediaAssetView = {
  id: 42,
  channel_id: 7,
  kind: 'video',
  source: 'studio_https',
  transport: 'https',
  label: 'Trailer',
  mime_type: 'video/mp4',
  width: 1280,
  height: 720,
  duration_seconds: 12,
  size_bytes: 123456,
  created_at: '2026-08-09T15:00:00Z',
};

describe('Rich media asset document boundary', () => {
  it('stores only durable identity and kind in a PostDocument block', () => {
    const patch = mediaAssetBlockPatch(asset);
    expect(patch).toEqual({ asset_id: 42, kind: 'video' });
    expect(JSON.stringify(patch)).not.toContain('https');
    expect(JSON.stringify(patch)).not.toContain('telegram_file_id');
    expect(JSON.stringify(patch)).not.toContain('storage_url');
  });

  it('clears asset identity without retaining stale transport data', () => {
    expect(mediaAssetBlockPatch(null)).toEqual({ asset_id: undefined, kind: 'photo' });
  });

  it('renders a useful redacted selector label', () => {
    expect(mediaAssetOptionLabel(asset)).toBe('Trailer · https');
  });

  it('creates collection items from durable identity only', () => {
    expect(mediaCollectionItem(asset)).toEqual({ type: 'media', asset_id: 42, kind: 'video' });
    expect(JSON.stringify(mediaCollectionItem(asset))).not.toContain('https');
  });

  it('sanitizes malformed collection input', () => {
    expect(mediaCollectionItems([
      { type: 'media', asset_id: 3, kind: 'photo', storage_url: 'https://secret.invalid/a' },
      { media_asset_id: '4', media_type: 'video' },
      { asset_id: 0, kind: 'photo' },
      null,
    ])).toEqual([
      { type: 'media', asset_id: 3, kind: 'photo' },
      { type: 'media', asset_id: 4, kind: 'video' },
    ]);
  });

  it('reorders without mutating the original array', () => {
    const items = [
      { type: 'media' as const, asset_id: 1, kind: 'photo' },
      { type: 'media' as const, asset_id: 2, kind: 'video' },
    ];
    const moved = moveMediaCollectionItem(items, 0, 1);
    expect(moved.map((item) => item.asset_id)).toEqual([2, 1]);
    expect(items.map((item) => item.asset_id)).toEqual([1, 2]);
  });
});
