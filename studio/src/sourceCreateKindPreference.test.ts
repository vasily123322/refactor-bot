import { describe, expect, it } from 'vitest';

import {
  parseSourceCreateKindPreference,
  serializeSourceCreateKindPreference,
} from './sourceCreateKindPreference';

describe('source create kind preference', () => {
  it('reads only visible source-create kind values from the UI envelope', () => {
    expect(parseSourceCreateKindPreference('{"sourceCreateKind":"rss"}')).toBe('rss');
    expect(parseSourceCreateKindPreference('{"sourceCreateKind":"url"}')).toBe('url');
    expect(parseSourceCreateKindPreference('{"sourceCreateKind":"telegram"}')).toBe('telegram');
    expect(parseSourceCreateKindPreference('{"sourceCreateKind":"telegram_channel_dms"}')).toBe(
      'telegram_channel_dms',
    );
  });

  it('fails closed for stale, malformed or missing values', () => {
    expect(parseSourceCreateKindPreference('{"sourceCreateKind":"web"}')).toBeNull();
    expect(parseSourceCreateKindPreference('{"sourceCreateKind":"RSS"}')).toBeNull();
    expect(parseSourceCreateKindPreference('{broken')).toBeNull();
    expect(parseSourceCreateKindPreference('[]')).toBeNull();
    expect(parseSourceCreateKindPreference(null)).toBeNull();
    expect(parseSourceCreateKindPreference(undefined)).toBeNull();
  });

  it('merges the source default without discarding other editor UI preferences', () => {
    expect(
      JSON.parse(
        serializeSourceCreateKindPreference(
          '{"previewDensity":"compact","sourceCreateKind":"rss"}',
          'telegram',
        ),
      ),
    ).toEqual({ previewDensity: 'compact', sourceCreateKind: 'telegram' });
  });

  it('recovers from an invalid existing envelope when persisting a new preference', () => {
    expect(JSON.parse(serializeSourceCreateKindPreference('{broken', 'url'))).toEqual({
      sourceCreateKind: 'url',
    });
  });
});
