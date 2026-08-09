import { describe, expect, it } from 'vitest';

import type { MediaAssetView } from './api';
import { mediaAssetBlockPatch, mediaAssetOptionLabel } from './richMediaAssets';

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
});
