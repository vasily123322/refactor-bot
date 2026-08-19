import { describe, expect, it, vi } from 'vitest';

import { createTelegramRequestChatBridge } from './telegramRequestChat';

describe('Telegram requestChat bridge', () => {
  it('fails closed below the Telegram 9.6 capability boundary', async () => {
    const getProvider = vi.fn(() => ({ requestChat: vi.fn() }));
    const requestChat = createTelegramRequestChatBridge({
      isSupported: () => false,
      getProvider,
    });

    await expect(requestChat('prepared-request')).resolves.toEqual({
      ok: false,
      status: 'unsupported',
    });
    expect(getProvider).not.toHaveBeenCalled();
  });

  it('fails safely when the official WebApp provider is absent', async () => {
    const requestChat = createTelegramRequestChatBridge({
      isSupported: () => true,
      getProvider: () => undefined,
    });

    await expect(requestChat('prepared-request')).resolves.toEqual({
      ok: false,
      status: 'unsupported',
    });
  });

  it('reports sent only after Telegram confirms the prepared request was sent', async () => {
    const provider = {
      requestChat: vi.fn((requestId: string, callback: (sent: boolean) => void) => {
        expect(requestId).toBe('prepared-request');
        callback(true);
      }),
    };
    const requestChat = createTelegramRequestChatBridge({
      isSupported: () => true,
      getProvider: () => provider,
    });

    await expect(requestChat('  prepared-request  ')).resolves.toEqual({
      ok: true,
      status: 'sent',
    });
    expect(provider.requestChat).toHaveBeenCalledTimes(1);
  });

  it('does not invent chat identity and reports Telegram false as not sent', async () => {
    const provider = {
      requestChat: vi.fn((_requestId: string, callback: (sent: boolean) => void) => {
        callback(false);
      }),
    };
    const requestChat = createTelegramRequestChatBridge({
      isSupported: () => true,
      getProvider: () => provider,
    });

    await expect(requestChat('prepared-request')).resolves.toEqual({
      ok: false,
      status: 'cancelled-or-not-sent',
    });
  });

  it('surfaces synchronous provider failures as explicit errors', async () => {
    const requestChat = createTelegramRequestChatBridge({
      isSupported: () => true,
      getProvider: () => ({
        requestChat() {
          throw new Error('requestChat provider failed');
        },
      }),
    });

    await expect(requestChat('prepared-request')).resolves.toEqual({
      ok: false,
      status: 'error',
      error: { name: 'Error', message: 'requestChat provider failed' },
    });
  });

  it('settles once if a provider invokes its callback more than once', async () => {
    const requestChat = createTelegramRequestChatBridge({
      isSupported: () => true,
      getProvider: () => ({
        requestChat(_requestId, callback) {
          callback(true);
          callback(false);
        },
      }),
    });

    await expect(requestChat('prepared-request')).resolves.toEqual({
      ok: true,
      status: 'sent',
    });
  });

  it('does not touch Telegram or open a picker during bridge construction', () => {
    const isSupported = vi.fn(() => true);
    const getProvider = vi.fn(() => undefined);
    createTelegramRequestChatBridge({ isSupported, getProvider });

    expect(isSupported).not.toHaveBeenCalled();
    expect(getProvider).not.toHaveBeenCalled();
  });
});
