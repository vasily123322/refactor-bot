import { describe, expect, it } from 'vitest';

import {
  boundedOptionalInteger,
  optionalText,
  richMediaAssetChangeCleanupPatch,
  richMediaOptionCapabilities,
  richMediaVisualBadges,
} from './richMediaOptions';

describe('rich single-media option policy', () => {
  it('exposes only renderer-supported options for each media kind', () => {
    expect(richMediaOptionCapabilities('photo')).toEqual({
      spoiler: true,
      dimensions: false,
      duration: false,
      streaming: false,
      audioMetadata: false,
    });
    expect(richMediaOptionCapabilities('video')).toEqual({
      spoiler: true,
      dimensions: true,
      duration: true,
      streaming: true,
      audioMetadata: false,
    });
    expect(richMediaOptionCapabilities('animation')).toEqual({
      spoiler: true,
      dimensions: true,
      duration: true,
      streaming: false,
      audioMetadata: false,
    });
    expect(richMediaOptionCapabilities('audio')).toEqual({
      spoiler: false,
      dimensions: false,
      duration: true,
      streaming: false,
      audioMetadata: true,
    });
    expect(richMediaOptionCapabilities('voice_note')).toEqual({
      spoiler: false,
      dimensions: false,
      duration: true,
      streaming: false,
      audioMetadata: false,
    });
  });

  it('resets asset-bound metadata and fields unsupported by the new asset kind', () => {
    expect(richMediaAssetChangeCleanupPatch('video')).toEqual({
      width: undefined,
      height: undefined,
      duration: undefined,
      performer: undefined,
      title: undefined,
    });
    expect(richMediaAssetChangeCleanupPatch('audio')).toEqual({
      has_spoiler: undefined,
      width: undefined,
      height: undefined,
      duration: undefined,
      supports_streaming: undefined,
      performer: undefined,
      title: undefined,
    });
    expect(richMediaAssetChangeCleanupPatch('photo')).toEqual({
      width: undefined,
      height: undefined,
      duration: undefined,
      supports_streaming: undefined,
      performer: undefined,
      title: undefined,
    });
  });

  it('accepts only renderer-bounded integer overrides', () => {
    expect(boundedOptionalInteger('', { minimum: 0, maximum: 86400 })).toBeUndefined();
    expect(boundedOptionalInteger('0', { minimum: 0, maximum: 86400 })).toBe(0);
    expect(boundedOptionalInteger('86400', { minimum: 0, maximum: 86400 })).toBe(86400);
    expect(boundedOptionalInteger('-1', { minimum: 0, maximum: 86400 })).toBeNull();
    expect(boundedOptionalInteger('86401', { minimum: 0, maximum: 86400 })).toBeNull();
    expect(boundedOptionalInteger('1.5', { minimum: 0, maximum: 86400 })).toBeNull();
  });

  it('trims optional text and removes blank values', () => {
    expect(optionalText('  ')).toBeUndefined();
    expect(optionalText(' Artist ')).toBe('Artist');
  });

  it('builds visual badges only from options meaningful for the media kind', () => {
    expect(richMediaVisualBadges({
      id: 'video-1',
      type: 'media',
      kind: 'video',
      has_spoiler: true,
      supports_streaming: true,
      duration: 12,
      width: 1280,
      height: 720,
      performer: 'ignored',
    })).toEqual(['spoiler', 'streaming', '12s', '1280×720']);

    expect(richMediaVisualBadges({
      id: 'audio-1',
      type: 'media',
      kind: 'audio',
      duration: 45,
      performer: 'Artist',
      title: 'Track',
      has_spoiler: true,
    })).toEqual(['45s', 'Artist', 'Track']);

    expect(richMediaVisualBadges({
      id: 'audio-2',
      type: 'media',
      kind: 'audio',
      duration: null,
    })).toEqual([]);
  });
});
