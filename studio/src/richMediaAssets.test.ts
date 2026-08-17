import { describe, expect, it } from 'vitest';

import type { MediaAssetView } from './api';
import {
  mediaAssetBlockPatch,
  mediaAssetOptionLabel,
  mediaCollectionItem,
  mediaCollectionItems,
  mediaCollectionItemWithAsset,
  moveMediaCollectionItem,
  patchMediaCollectionItem,
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

const animationAsset: MediaAssetView = {
  ...asset,
  id: 43,
  kind: 'animation',
  label: 'Loop',
  width: 640,
  height: 640,
  duration_seconds: 5,
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

  it('preserves safe renderer options and rich captions while stripping transport fields', () => {
    const items = mediaCollectionItems([
      {
        type: 'media',
        asset_id: 3,
        kind: 'photo',
        storage_url: 'https://secret.invalid/a',
        has_spoiler: true,
        duration: 99,
        caption: {
          text: [{ text: 'Photo', marks: ['bold'] }],
          credit: [{ text: 'Source', marks: ['italic'] }],
        },
      },
      {
        media_asset_id: '4',
        media_type: 'video',
        telegram_file_id: 'secret-file-id',
        supports_streaming: true,
        duration: '12',
        width: 1280,
        height: 720,
        performer: 'ignored',
      },
      { asset_id: 0, kind: 'photo' },
      null,
    ]);

    expect(items).toEqual([
      {
        type: 'media',
        asset_id: 3,
        kind: 'photo',
        caption: [{ text: 'Photo', marks: ['bold'] }],
        credit: [{ text: 'Source', marks: ['italic'] }],
        has_spoiler: true,
      },
      {
        type: 'media',
        asset_id: 4,
        kind: 'video',
        supports_streaming: true,
        width: 1280,
        height: 720,
        duration: 12,
      },
    ]);
    const serialized = JSON.stringify(items);
    expect(serialized).not.toContain('secret.invalid');
    expect(serialized).not.toContain('secret-file-id');
    expect(serialized).not.toContain('performer');
  });

  it('resets asset-bound metadata while preserving caption and compatible presentation options', () => {
    const current = mediaCollectionItems([{
      type: 'media',
      asset_id: 42,
      kind: 'video',
      has_spoiler: true,
      supports_streaming: true,
      width: 1920,
      height: 1080,
      duration: 30,
      caption: [{ text: 'Keep caption', marks: ['bold'] }],
      credit: 'Keep credit',
    }])[0];

    expect(mediaCollectionItemWithAsset(current, animationAsset)).toEqual({
      type: 'media',
      asset_id: 43,
      kind: 'animation',
      caption: [{ text: 'Keep caption', marks: ['bold'] }],
      credit: 'Keep credit',
      has_spoiler: true,
    });
  });

  it('patches one item through the same safe parser boundary', () => {
    const current = mediaCollectionItem(asset);
    const next = patchMediaCollectionItem(current, {
      has_spoiler: true,
      supports_streaming: true,
      duration: 15,
      storage_url: 'https://secret.invalid/b',
    });
    expect(next).toEqual({
      type: 'media',
      asset_id: 42,
      kind: 'video',
      has_spoiler: true,
      supports_streaming: true,
      duration: 15,
    });
    expect(JSON.stringify(next)).not.toContain('secret.invalid');
  });

  it('reorders without mutating or stripping item options', () => {
    const items = mediaCollectionItems([
      { type: 'media', asset_id: 1, kind: 'photo', has_spoiler: true },
      { type: 'media', asset_id: 2, kind: 'video', duration: 12 },
    ]);
    const moved = moveMediaCollectionItem(items, 0, 1);
    expect(moved).toEqual([
      { type: 'media', asset_id: 2, kind: 'video', duration: 12 },
      { type: 'media', asset_id: 1, kind: 'photo', has_spoiler: true },
    ]);
    expect(items[0]).toEqual({ type: 'media', asset_id: 1, kind: 'photo', has_spoiler: true });
  });
});
