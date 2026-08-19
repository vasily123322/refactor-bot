import type { MediaAssetKind } from './api';
import type { PostBlock } from './types';

export type RichMediaOptionCapabilities = {
  spoiler: boolean;
  dimensions: boolean;
  duration: boolean;
  streaming: boolean;
  audioMetadata: boolean;
};

const MEDIA_KINDS = new Set<MediaAssetKind>([
  'photo',
  'video',
  'animation',
  'audio',
  'voice_note',
]);

const ALL_OPTION_FIELDS = [
  'has_spoiler',
  'width',
  'height',
  'duration',
  'supports_streaming',
  'performer',
  'title',
] as const;

const ASSET_BOUND_FIELDS = new Set<string>([
  'width',
  'height',
  'duration',
  'performer',
  'title',
]);

const ALLOWED_FIELDS: Record<MediaAssetKind, ReadonlySet<string>> = {
  photo: new Set(['has_spoiler']),
  video: new Set(['has_spoiler', 'width', 'height', 'duration', 'supports_streaming']),
  animation: new Set(['has_spoiler', 'width', 'height', 'duration']),
  audio: new Set(['duration', 'performer', 'title']),
  voice_note: new Set(['duration']),
};

export function richMediaKind(value: unknown): MediaAssetKind {
  const kind = String(value ?? '').trim().toLowerCase() as MediaAssetKind;
  return MEDIA_KINDS.has(kind) ? kind : 'photo';
}

export function richMediaOptionCapabilities(kindValue: unknown): RichMediaOptionCapabilities {
  const kind = richMediaKind(kindValue);
  return {
    spoiler: kind === 'photo' || kind === 'video' || kind === 'animation',
    dimensions: kind === 'video' || kind === 'animation',
    duration: kind !== 'photo',
    streaming: kind === 'video',
    audioMetadata: kind === 'audio',
  };
}

export function richMediaAssetChangeCleanupPatch(kindValue: unknown): Partial<PostBlock> {
  const kind = richMediaKind(kindValue);
  const allowed = ALLOWED_FIELDS[kind];
  const patch: Partial<PostBlock> = {};
  for (const field of ALL_OPTION_FIELDS) {
    if (ASSET_BOUND_FIELDS.has(field) || !allowed.has(field)) patch[field] = undefined;
  }
  return patch;
}

export function boundedOptionalInteger(
  raw: string,
  bounds: { minimum: number; maximum: number },
): number | undefined | null {
  if (raw.trim() === '') return undefined;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < bounds.minimum || value > bounds.maximum) return null;
  return value;
}

export function optionalText(raw: string): string | undefined {
  const value = raw.trim();
  return value || undefined;
}

function blockInteger(value: unknown, minimum: number, maximum: number): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string' && value.trim() === '') return null;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) return null;
  return parsed;
}

export function richMediaVisualBadges(block: PostBlock): string[] {
  const kind = richMediaKind(block.kind);
  const capabilities = richMediaOptionCapabilities(kind);
  const badges: string[] = [];
  if (capabilities.spoiler && block.has_spoiler === true) badges.push('spoiler');
  if (capabilities.streaming && block.supports_streaming === true) badges.push('streaming');
  if (capabilities.duration) {
    const duration = blockInteger(block.duration, 0, 86400);
    if (duration !== null) badges.push(`${duration}s`);
  }
  if (capabilities.dimensions) {
    const width = blockInteger(block.width, 1, 10000);
    const height = blockInteger(block.height, 1, 10000);
    if (width !== null && height !== null) badges.push(`${width}×${height}`);
  }
  if (capabilities.audioMetadata) {
    const performer = typeof block.performer === 'string' ? block.performer.trim() : '';
    const title = typeof block.title === 'string' ? block.title.trim() : '';
    if (performer) badges.push(performer);
    if (title) badges.push(title);
  }
  return badges;
}
