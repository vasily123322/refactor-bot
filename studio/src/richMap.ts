import type { PostBlock } from './types';

export type RichMapDraft = {
  latitude: number;
  longitude: number;
  zoom: number;
  width: number;
  height: number;
  caption: string;
};

export const DEFAULT_RICH_MAP: RichMapDraft = {
  latitude: 55.7558,
  longitude: 37.6176,
  zoom: 13,
  width: 640,
  height: 360,
  caption: '',
};

function finiteNumber(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function richMapDraft(block: PostBlock): RichMapDraft {
  return {
    latitude: finiteNumber(block.latitude ?? block.lat, DEFAULT_RICH_MAP.latitude),
    longitude: finiteNumber(
      block.longitude ?? block.lon ?? block.lng,
      DEFAULT_RICH_MAP.longitude,
    ),
    zoom: finiteNumber(block.zoom, DEFAULT_RICH_MAP.zoom),
    width: finiteNumber(block.width, DEFAULT_RICH_MAP.width),
    height: finiteNumber(block.height, DEFAULT_RICH_MAP.height),
    caption: typeof block.caption === 'string' ? block.caption : '',
  };
}

export function richMapPatch(draft: RichMapDraft): Partial<PostBlock> {
  return {
    latitude: draft.latitude,
    longitude: draft.longitude,
    zoom: draft.zoom,
    width: draft.width,
    height: draft.height,
    caption: draft.caption,
  };
}

export function richMapError(draft: RichMapDraft): string | null {
  if (!Number.isFinite(draft.latitude) || draft.latitude < -90 || draft.latitude > 90) {
    return 'Широта должна быть от -90 до 90';
  }
  if (!Number.isFinite(draft.longitude) || draft.longitude < -180 || draft.longitude > 180) {
    return 'Долгота должна быть от -180 до 180';
  }
  if (!Number.isInteger(draft.zoom) || draft.zoom < 0 || draft.zoom > 24) {
    return 'Zoom должен быть целым числом от 0 до 24';
  }
  if (!Number.isInteger(draft.width) || draft.width < 1 || draft.width > 10000) {
    return 'Ширина должна быть целым числом от 1 до 10000';
  }
  if (!Number.isInteger(draft.height) || draft.height < 1 || draft.height > 10000) {
    return 'Высота должна быть целым числом от 1 до 10000';
  }
  if (draft.width + draft.height > 10000) {
    return 'Сумма ширины и высоты не должна превышать 10000';
  }
  const ratio = Math.max(draft.width / draft.height, draft.height / draft.width);
  if (ratio > 20) {
    return 'Соотношение сторон карты не должно превышать 20:1';
  }
  return null;
}
