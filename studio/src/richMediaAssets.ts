import type { MediaAssetView } from './api';
import type { PostBlock } from './types';

export type MediaCollectionItem = {
  type: 'media';
  asset_id: number;
  kind: string;
};

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

export function mediaCollectionItem(asset: MediaAssetView): MediaCollectionItem {
  return {
    type: 'media',
    asset_id: asset.id,
    kind: asset.kind,
  };
}

export function mediaCollectionItems(value: unknown): MediaCollectionItem[] {
  if (!Array.isArray(value)) return [];
  const items: MediaCollectionItem[] = [];
  for (const raw of value) {
    if (typeof raw !== 'object' || raw === null) continue;
    const candidate = raw as Record<string, unknown>;
    const assetId = Number(candidate.asset_id ?? candidate.media_asset_id ?? 0);
    if (!Number.isInteger(assetId) || assetId <= 0) continue;
    const kind = String(candidate.kind ?? candidate.media_type ?? 'photo').trim().toLowerCase();
    if (!kind) continue;
    items.push({ type: 'media', asset_id: assetId, kind });
  }
  return items;
}

export function moveMediaCollectionItem(
  items: MediaCollectionItem[],
  index: number,
  direction: -1 | 1,
): MediaCollectionItem[] {
  const target = index + direction;
  if (index < 0 || index >= items.length || target < 0 || target >= items.length) {
    return [...items];
  }
  const next = [...items];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}
