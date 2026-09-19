import type { MediaAssetView } from './api';

export const MEDIA_ASSET_HISTORY_PAGE_SIZE = 100;

export type MediaAssetHistoryCursor = {
  createdAt: string;
  id: number;
};

export function splitMediaAssetHistoryPage(
  rows: MediaAssetView[],
  pageSize = MEDIA_ASSET_HISTORY_PAGE_SIZE,
): { items: MediaAssetView[]; hasMore: boolean } {
  const boundedSize = Math.max(1, pageSize);
  return {
    items: rows.slice(0, boundedSize),
    hasMore: rows.length > boundedSize,
  };
}

export function mediaAssetHistoryCursor(
  items: MediaAssetView[],
): MediaAssetHistoryCursor | null {
  const last = items.at(-1);
  if (!last?.created_at) return null;
  return { createdAt: last.created_at, id: last.id };
}

export function mergeMediaAssetHistoryPage(
  current: MediaAssetView[],
  incoming: MediaAssetView[],
): MediaAssetView[] {
  const seen = new Set(current.map((asset) => asset.id));
  return [
    ...current,
    ...incoming.filter((asset) => {
      if (seen.has(asset.id)) return false;
      seen.add(asset.id);
      return true;
    }),
  ];
}

export function prependMediaAsset(
  current: MediaAssetView[],
  created: MediaAssetView,
): MediaAssetView[] {
  return [created, ...current.filter((asset) => asset.id !== created.id)];
}
