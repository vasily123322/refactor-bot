import type { MediaAssetView } from './api';
import {
  richMediaAssetChangeCleanupPatch,
  richMediaOptionCapabilities,
} from './richMediaOptions';
import type { PostBlock, RichContentValue } from './types';

export type MediaCollectionItem = {
  type: 'media';
  asset_id: number;
  kind: string;
  caption?: RichContentValue;
  credit?: RichContentValue;
  has_spoiler?: boolean;
  supports_streaming?: boolean;
  width?: number;
  height?: number;
  duration?: number;
  performer?: string;
  title?: string;
};

function safeRichContent(value: unknown): RichContentValue | undefined {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return structuredClone(value) as RichContentValue;
  return undefined;
}

function boundedInteger(value: unknown, minimum: number, maximum: number): number | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === 'string' && value.trim() === '') return undefined;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) return undefined;
  return parsed;
}

function optionalTrimmedText(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const text = value.trim();
  return text || undefined;
}

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

    const item: MediaCollectionItem = { type: 'media', asset_id: assetId, kind };
    const capabilities = richMediaOptionCapabilities(kind);
    const caption = safeRichContent(candidate.caption);
    const credit = safeRichContent(candidate.credit);
    if (caption !== undefined) item.caption = caption;
    if (credit !== undefined) item.credit = credit;

    if (capabilities.spoiler && typeof candidate.has_spoiler === 'boolean') {
      item.has_spoiler = candidate.has_spoiler;
    }
    if (capabilities.streaming && typeof candidate.supports_streaming === 'boolean') {
      item.supports_streaming = candidate.supports_streaming;
    }
    if (capabilities.dimensions) {
      const width = boundedInteger(candidate.width, 1, 10000);
      const height = boundedInteger(candidate.height, 1, 10000);
      if (width !== undefined) item.width = width;
      if (height !== undefined) item.height = height;
    }
    if (capabilities.duration) {
      const duration = boundedInteger(candidate.duration, 0, 86400);
      if (duration !== undefined) item.duration = duration;
    }
    if (capabilities.audioMetadata) {
      const performer = optionalTrimmedText(candidate.performer);
      const title = optionalTrimmedText(candidate.title);
      if (performer !== undefined) item.performer = performer;
      if (title !== undefined) item.title = title;
    }
    items.push(item);
  }
  return items;
}

export function mediaCollectionItemWithAsset(
  item: MediaCollectionItem,
  asset: MediaAssetView,
): MediaCollectionItem {
  const [next] = mediaCollectionItems([{
    ...item,
    ...mediaCollectionItem(asset),
    ...richMediaAssetChangeCleanupPatch(asset.kind),
  }]);
  return next ?? mediaCollectionItem(asset);
}

export function patchMediaCollectionItem(
  item: MediaCollectionItem,
  patch: Partial<PostBlock>,
): MediaCollectionItem {
  const [next] = mediaCollectionItems([{ ...item, ...patch }]);
  return next ?? item;
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
