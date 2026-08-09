import type { MediaAssetView } from './api';
import type { PostBlock } from './types';

export function mediaAssetOptionLabel(asset: MediaAssetView): string {
  const title = asset.label?.trim() || `${asset.kind} #${asset.id}`;
  return `${title} · ${asset.transport}`;
}

export function mediaAssetBlockPatch(asset: MediaAssetView | null): Partial<PostBlock> {
  if (!asset) {
    return { asset_id: undefined, kind: 'photo' };
  }
  return {
    asset_id: asset.id,
    kind: asset.kind,
  };
}
