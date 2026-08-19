import { describe, expect, it } from 'vitest';

import { channelOnboardingResultMessage } from './channelOnboardingMessages';

describe('channel onboarding messages', () => {
  it('shows success only for server-verified channel success', () => {
    expect(
      channelOnboardingResultMessage({ ok: true, status: 'succeeded', channelId: 77 }),
    ).toEqual({ kind: 'success', text: 'Канал подключён.' });
  });

  it('keeps native picker cancellation neutral', () => {
    expect(
      channelOnboardingResultMessage({ ok: false, status: 'cancelled' }),
    ).toEqual({ kind: 'notice', text: 'Выбор канала отменён.' });
  });

  it('maps server authority failures without treating native selection as proof', () => {
    expect(
      channelOnboardingResultMessage({
        ok: false,
        status: 'server-failed',
        failureReason: 'bot-missing-rights',
      }),
    ).toEqual({
      kind: 'error',
      text: 'Боту нужны права публикации, редактирования и удаления.',
    });
    expect(
      channelOnboardingResultMessage({
        ok: false,
        status: 'server-failed',
        failureReason: 'owner-conflict',
      }),
    ).toEqual({
      kind: 'error',
      text: 'Этот канал уже связан с другим владельцем Studio.',
    });
  });

  it('distinguishes sent-peer timeout from picker cancellation', () => {
    expect(
      channelOnboardingResultMessage({ ok: false, status: 'timeout' }).kind,
    ).toBe('error');
  });
});
