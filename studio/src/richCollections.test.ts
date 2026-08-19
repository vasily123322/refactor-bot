import { describe, expect, it } from 'vitest';

import {
  isRichCollectionBlockType,
  newRichCollectionBlock,
  RICH_COLLECTION_BLOCK_OPTIONS,
  richCollectionPresentation,
  richCollectionVisualModel,
} from './richCollections';

describe('rich collection block policy', () => {
  it('advertises every collection type supported by the Telegram renderer', () => {
    expect(RICH_COLLECTION_BLOCK_OPTIONS.map(([type]) => type)).toEqual([
      'gallery',
      'collage',
      'slideshow',
    ]);
  });

  it('classifies only collection blocks', () => {
    expect(isRichCollectionBlockType('gallery')).toBe(true);
    expect(isRichCollectionBlockType('collage')).toBe(true);
    expect(isRichCollectionBlockType('slideshow')).toBe(true);
    expect(isRichCollectionBlockType('media')).toBe(false);
    expect(isRichCollectionBlockType('map')).toBe(false);
  });

  it('creates the same publishable initial shape for every collection type', () => {
    for (const type of ['gallery', 'collage', 'slideshow'] as const) {
      expect(newRichCollectionBlock(`${type}-1`, type)).toEqual({
        id: `${type}-1`,
        type,
        items: [],
        caption: '',
      });
    }
    expect(newRichCollectionBlock('media-1', 'media')).toBeNull();
  });

  it('normalizes collection items for visual preview without inventing a second schema', () => {
    const model = richCollectionVisualModel({
      id: 'col-1',
      type: 'collage',
      items: [
        { type: 'media', asset_id: 11, kind: 'photo' },
        { media_asset_id: 12, media_type: 'video' },
        { asset_id: 0, kind: 'photo' },
      ],
      caption: 'Caption',
    });

    expect(model).toEqual({
      type: 'collage',
      label: 'Коллаж',
      icon: '▥',
      items: [
        { type: 'media', asset_id: 11, kind: 'photo' },
        { type: 'media', asset_id: 12, kind: 'video' },
      ],
    });
    expect(richCollectionVisualModel({ id: 'media-1', type: 'media' })).toBeNull();
  });

  it('provides stable visual labels for all collection types', () => {
    expect(richCollectionPresentation('gallery')).toEqual({ label: 'Галерея', icon: '▦' });
    expect(richCollectionPresentation('collage')).toEqual({ label: 'Коллаж', icon: '▥' });
    expect(richCollectionPresentation('slideshow')).toEqual({ label: 'Слайдшоу', icon: '▤' });
  });
});
