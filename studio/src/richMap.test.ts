import { describe, expect, it } from 'vitest';

import { DEFAULT_RICH_MAP, richMapDraft, richMapError, richMapPatch } from './richMap';

describe('Rich map editor contract', () => {
  it('normalizes legacy coordinate aliases into canonical fields', () => {
    const draft = richMapDraft({
      id: 'map',
      type: 'map',
      lat: 48.8566,
      lng: 2.3522,
      zoom: 12,
      width: 640,
      height: 360,
      caption: 'Paris',
    });
    expect(draft).toEqual({
      latitude: 48.8566,
      longitude: 2.3522,
      zoom: 12,
      width: 640,
      height: 360,
      caption: 'Paris',
    });
    expect(richMapPatch(draft)).toEqual(draft);
  });

  it('falls back only for non-numeric values', () => {
    expect(richMapDraft({ id: 'map', type: 'map', latitude: 'bad' }).latitude).toBe(
      DEFAULT_RICH_MAP.latitude,
    );
  });

  it('matches renderer bounds and fails closed', () => {
    expect(richMapError({ ...DEFAULT_RICH_MAP, latitude: 91 })).toMatch(/Широта/);
    expect(richMapError({ ...DEFAULT_RICH_MAP, longitude: -181 })).toMatch(/Долгота/);
    expect(richMapError({ ...DEFAULT_RICH_MAP, zoom: 24 })).toBeNull();
    expect(richMapError({ ...DEFAULT_RICH_MAP, zoom: 25 })).toMatch(/Zoom/);
    expect(richMapError({ ...DEFAULT_RICH_MAP, width: 9000, height: 2000 })).toMatch(/Сумма/);
    expect(richMapError({ ...DEFAULT_RICH_MAP, width: 1000, height: 40 })).toMatch(/20:1/);
    expect(richMapError(DEFAULT_RICH_MAP)).toBeNull();
  });
});
