import { describe, expect, it, vi } from 'vitest';

import {
  runNativeChannelOnboarding,
  type ChannelOnboardingPrepare,
  type ChannelOnboardingStatus,
} from './channelOnboardingFlow';
import type { TelegramRequestChatResult } from './telegramRequestChat';

function baseDeps() {
  return {
    prepare: vi.fn(async (): Promise<ChannelOnboardingPrepare> => ({
      request_id: 1_234_567,
      prepared_button_id: 'prepared-native-id',
      status: 'prepared',
      expires_at: '2026-08-17T15:30:00Z',
    })),
    getStatus: vi.fn(async (): Promise<ChannelOnboardingStatus> => ({
      request_id: 1_234_567,
      status: 'processing',
      expires_at: '2026-08-17T15:30:00Z',
      channel_id: null,
      failure_reason: null,
    })),
    cancel: vi.fn(async (): Promise<ChannelOnboardingStatus> => ({
      request_id: 1_234_567,
      status: 'cancelled',
      expires_at: '2026-08-17T15:30:00Z',
      channel_id: null,
      failure_reason: 'cancelled',
    })),
    requestNative: vi.fn(
      async (): Promise<TelegramRequestChatResult> => ({ ok: true, status: 'sent' }),
    ),
    sleep: vi.fn(async () => undefined),
  };
}

describe('native onboarding client correlation', () => {
  it('rejects malformed prepare state before opening Telegram picker', async () => {
    const deps = baseDeps();
    deps.prepare.mockResolvedValueOnce({
      request_id: 1_234_567,
      prepared_button_id: 'prepared-native-id',
      status: 'reserved',
      expires_at: '2026-08-17T15:30:00Z',
    });
    await expect(runNativeChannelOnboarding(deps)).resolves.toEqual({
      ok: false,
      status: 'server-error',
    });
    expect(deps.requestNative).not.toHaveBeenCalled();
    expect(deps.cancel).toHaveBeenCalledWith(1_234_567);
  });

  it('rejects a malformed prepared button id from runtime JSON', async () => {
    const deps = baseDeps();
    deps.prepare.mockResolvedValueOnce({
      request_id: 1_234_567,
      prepared_button_id: 42,
      status: 'prepared',
      expires_at: '2026-08-17T15:30:00Z',
    } as unknown as ChannelOnboardingPrepare);
    await expect(runNativeChannelOnboarding(deps)).resolves.toEqual({
      ok: false,
      status: 'server-error',
    });
    expect(deps.requestNative).not.toHaveBeenCalled();
  });

  it('rejects a status response correlated to another request id', async () => {
    const deps = baseDeps();
    deps.getStatus.mockResolvedValueOnce({
      request_id: 7_654_321,
      status: 'succeeded',
      expires_at: '2026-08-17T15:30:00Z',
      channel_id: 99,
      failure_reason: null,
    });
    await expect(
      runNativeChannelOnboarding(deps, { pollIntervalMs: 0, maxPolls: 1 }),
    ).resolves.toEqual({ ok: false, status: 'server-error' });
    expect(deps.requestNative).toHaveBeenCalledTimes(1);
  });

  it('requires a positive verified server channel id before product refresh', async () => {
    const deps = baseDeps();
    deps.getStatus.mockResolvedValueOnce({
      request_id: 1_234_567,
      status: 'succeeded',
      expires_at: '2026-08-17T15:30:00Z',
      channel_id: 0,
      failure_reason: null,
    });
    await expect(
      runNativeChannelOnboarding(deps, { pollIntervalMs: 0, maxPolls: 1 }),
    ).resolves.toEqual({
      ok: false,
      status: 'server-failed',
      failureReason: 'missing-channel-id',
    });
  });
});
