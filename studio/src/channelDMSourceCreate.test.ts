import { describe, expect, it } from 'vitest';

import {
  buildSourceCreateInput,
  CHANNEL_DM_SOURCE_KIND,
  hasChannelDMSource,
  sourceCreateNeedsValue,
} from './channelDMSourceCreate';

describe('Channel-DM source create adaptation', () => {
  it('derives the connector value from the current owned channel instead of typed input', () => {
    const input = buildSourceCreateInput({
      kind: CHANNEL_DM_SOURCE_KIND,
      value: 'attacker-controlled-value',
      channelTgChatId: -1001234567890,
      mode: 'summary',
      citationEnabled: true,
      reusePolicy: 'reference_only',
    });

    expect(input).not.toBeNull();
    expect(input!.kind).toBe(CHANNEL_DM_SOURCE_KIND);
    expect(input!.value).toBe('-1001234567890');
  });

  it('keeps ordinary source value validation unchanged', () => {
    expect(sourceCreateNeedsValue('rss')).toBe(true);
    expect(buildSourceCreateInput({
      kind: 'rss',
      value: '   ',
      channelTgChatId: 1,
      mode: 'summary',
      citationEnabled: true,
      reusePolicy: 'reference_only',
    })).toBeNull();
  });

  it('marks the explicit Channel-DM connector as singleton in the normal Studio flow', () => {
    expect(hasChannelDMSource(['rss', CHANNEL_DM_SOURCE_KIND])).toBe(true);
    expect(hasChannelDMSource(['rss', 'telegram'])).toBe(false);
  });
});
