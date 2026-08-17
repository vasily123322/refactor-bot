import { mediaCollectionItems, type MediaCollectionItem } from './richMediaAssets';
import type { PostBlock } from './types';

export const RICH_COLLECTION_BLOCK_OPTIONS = [
  ['gallery', '▦ Галерея'],
  ['collage', '▥ Коллаж'],
  ['slideshow', '▤ Слайдшоу'],
] as const;

export type RichCollectionBlockType = (typeof RICH_COLLECTION_BLOCK_OPTIONS)[number][0];

export type RichCollectionPresentation = {
  label: string;
  icon: string;
};

export type RichCollectionVisualModel = RichCollectionPresentation & {
  type: RichCollectionBlockType;
  items: MediaCollectionItem[];
};

const RICH_COLLECTION_TYPES = new Set<string>(
  RICH_COLLECTION_BLOCK_OPTIONS.map(([type]) => type),
);

const RICH_COLLECTION_PRESENTATION: Record<RichCollectionBlockType, RichCollectionPresentation> = {
  gallery: { label: 'Галерея', icon: '▦' },
  collage: { label: 'Коллаж', icon: '▥' },
  slideshow: { label: 'Слайдшоу', icon: '▤' },
};

export function isRichCollectionBlockType(type: string): type is RichCollectionBlockType {
  return RICH_COLLECTION_TYPES.has(type);
}

export function newRichCollectionBlock(id: string, type: string): PostBlock | null {
  if (!isRichCollectionBlockType(type)) return null;
  return { id, type, items: [], caption: '' };
}

export function richCollectionPresentation(type: RichCollectionBlockType): RichCollectionPresentation {
  return RICH_COLLECTION_PRESENTATION[type];
}

export function richCollectionVisualModel(block: PostBlock): RichCollectionVisualModel | null {
  if (!isRichCollectionBlockType(block.type)) return null;
  return {
    type: block.type,
    ...richCollectionPresentation(block.type),
    items: mediaCollectionItems(block.items),
  };
}
