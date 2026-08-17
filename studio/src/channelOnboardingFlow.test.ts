import { describe, expect, it, vi } from 'vitest';

import {
  runNativeChannelOnboarding,
  type ChannelOnboardingStatus,
} from './channelOnboardingFlow';
import type { TelegramRequestChatResult } from './telegramRequestChat';

function prepared() {
  return {
    request_id: 1_234_567,
    prepared_button_id: 'prepared-native-id',
    status: 'prepared' as const,
    expires_at: '2026-08-17T15:30:00Z',
  };
}

function status(
  value: ChannelOnboardingStatus['status'],
  overrides: Partial<ChannelOnboardingStatus> = {},
): ChannelOnboardingStatus {
  return {
    request_id: 1_234_567,
    status: value,
    expires_at: '2026-08-17T15:30:00Z',
    channel_id: null,
    failure_reason: null,
    ...overrides,
  };
}

function deps(statuses: ChannelOnboardingStatus[] = []) {
  let index = 0;
  return {
    prepare: vi.fn(async () => prepared()),
    getStatus: vi.fn(async (): Promise<ChannelOnboardingStatus> => {
      if (statuses.length === 0) return status('prepared');
      return statuses[Math.min(index++, statuses.length - 1)];
    }),
    cancel: vi.fn(async () => status('cancelled', { failure_reason: 'cancelled' })),
    requestNative: vi.fn(
      async (): Promise<TelegramRequestChatResult> => ({ ok: true, status: 'sent' }),
    ),
    sleep: vi.fn(async () => undefined),
  };
}

describe('native channel onboarding flow', () => {
  it('refreshes only after server terminal success with a verified channel id', async () => {
    const bridge = deps([
      status('prepared'),
      status('processing'),
      status('succeeded', { channel_id: 77 }),
    ]);
    await expect(
      runNativeChannelOnboarding(bridge, { pollIntervalMs: 0, maxPolls: 5 }),
    ).resolves.toEqual({ ok: true, status: 'succeeded', channelId: 77 });
    expect(bridge.requestNative).toHaveBeenCalledWith('prepared-native-id');
    expect(bridge.cancel).not.toHaveBeenCalled();
  });

  it('cancels the server request when the native picker is unsupported', async () => {
    const bridge = deps();
    bridge.requestNative.mockResolvedValueOnce({ ok: false, status: 'unsupported' });
    await expect(runNativeChannelOnboarding(bridge)).resolves.toEqual({
      ok: false,
      status: 'unsupported',
    });
    expect(bridge.cancel).toHaveBeenCalledWith(1_234_567);
    expect(bridge.getStatus).not.toHaveBeenCalled();
  });

  it('cancels prepared state when the user cancels or Telegram does not send the peer', async () => {
    const bridge = deps();
    bridge.requestNative.mockResolvedValueOnce({
      ok: false,
      status: 'cancelled-or-not-sent',
    });
    await expect(runNativeChannelOnboarding(bridge)).resolves.toEqual({
      ok: false,
      status: 'cancelled',
    });
    expect(bridge.cancel).toHaveBeenCalledTimes(1);
  });

  it('surfaces server authority rejection without retrying the native picker', async () => {
    const bridge = deps([
      status('processing'),
      status('failed', { failure_reason: 'bot-missing-rights' }),
    ]);
    await expect(
      runNativeChannelOnboarding(bridge, { pollIntervalMs: 0, maxPolls: 3 }),
    ).resolves.toEqual({
      ok: false,
      status: 'server-failed',
      failureReason: 'bot-missing-rights',
    });
    expect(bridge.requestNative).toHaveBeenCalledTimes(1);
  });

  it('treats succeeded-without-channel-id as a fail-closed server result', async () => {
    const bridge = deps([status('succeeded')]);
    await expect(
      runNativeChannelOnboarding(bridge, { pollIntervalMs: 0, maxPolls: 1 }),
    ).resolves.toEqual({
      ok: false,
      status: 'server-failed',
      failureReason: 'missing-channel-id',
    });
  });

  it('bounds polling after a sent peer and cancels still-prepared work best-effort', async () => {
    const bridge = deps([status('prepared')]);
    await expect(
      runNativeChannelOnboarding(bridge, { pollIntervalMs: 0, maxPolls: 2 }),
    ).resolves.toEqual({ ok: false, status: 'timeout' });
    expect(bridge.getStatus).toHaveBeenCalledTimes(3);
    expect(bridge.cancel).toHaveBeenCalledTimes(1);
  });

  it('does not cancel a peer already sent to the bot when the UI aborts polling', async () => {
    const bridge = deps([status('processing')]);
    const controller = new AbortController();
    bridge.sleep.mockImplementationOnce(async () => {
      controller.abort();
    });
    await expect(
      runNativeChannelOnboarding(bridge, {
        signal: controller.signal,
        pollIntervalMs: 0,
        maxPolls: 3,
      }),
    ).resolves.toEqual({ ok: false, status: 'aborted' });
    expect(bridge.cancel).not.toHaveBeenCalled();
  });

  it('reports prepare/network failure without opening the native picker', async () => {
    const bridge = deps();
    bridge.prepare.mockRejectedValueOnce(new Error('network down'));
    await expect(runNativeChannelOnboarding(bridge)).resolves.toEqual({
      ok: false,
      status: 'server-error',
    });
    expect(bridge.requestNative).not.toHaveBeenCalled();
  });
});
