import { describe, expect, it, vi } from 'vitest';

const sdk = vi.hoisted(() => {
  const available = (
    implementation: (...args: any[]) => any,
    supported = false,
  ) => Object.assign(vi.fn(implementation), { isAvailable: vi.fn(() => supported) });

  const asyncAvailable = (
    implementation: (...args: any[]) => Promise<any>,
    supported = false,
  ) => Object.assign(vi.fn(implementation), { isAvailable: vi.fn(() => supported) });

  const button = () => ({
    isSupported: vi.fn(() => false),
    isMounted: vi.fn(() => false),
    mount: available(() => undefined),
    unmount: vi.fn(),
    onClick: available((_listener: VoidFunction) => vi.fn()),
    show: available(() => undefined),
    hide: available(() => undefined),
    setText: available((_text: string) => undefined),
    setPosition: available((_position: string) => undefined),
  });

  const backButton = button();
  const mainButton = button();
  const secondaryButton = button();

  return {
    init: vi.fn(),
    setDebug: vi.fn(),
    miniApp: {
      mount: available(() => undefined),
    },
    themeParams: {
      mount: vi.fn(),
      bindCssVars: vi.fn(),
    },
    viewport: {
      isMounted: vi.fn(() => false),
      isFullscreen: vi.fn(() => false),
      mount: asyncAvailable(async () => undefined),
      bindCssVars: vi.fn(),
      requestFullscreen: asyncAvailable(async () => undefined),
      exitFullscreen: asyncAvailable(async () => undefined),
    },
    closingBehavior: {
      isMounted: vi.fn(() => false),
      isConfirmationEnabled: vi.fn(() => false),
      mount: available(() => undefined),
      unmount: vi.fn(),
      enableConfirmation: available(() => undefined),
      disableConfirmation: available(() => undefined),
    },
    hapticFeedback: {
      impactOccurred: available((_style: string) => undefined),
      notificationOccurred: available((_type: string) => undefined),
      selectionChanged: available(() => undefined),
    },
    backButton,
    mainButton,
    secondaryButton,
    deviceStorage: {
      getItem: asyncAvailable(async (_key: string) => null),
      setItem: asyncAvailable(async (_key: string, _value: string | null) => undefined),
      deleteItem: asyncAvailable(async (_key: string) => undefined),
      clear: asyncAvailable(async () => undefined),
    },
    secureStorage: {
      getItem: asyncAvailable(async (_key: string) => ({ value: null, canRestore: false })),
      setItem: asyncAvailable(async (_key: string, _value: string | null) => undefined),
      deleteItem: asyncAvailable(async (_key: string) => undefined),
      clear: asyncAvailable(async () => undefined),
      restoreItem: asyncAvailable(async (_key: string) => null),
    },
    shareMessage: asyncAvailable(async (_id: string) => undefined),
    downloadFile: asyncAvailable(async (_url: string, _fileName: string) => undefined),
  };
});

vi.mock('@tma.js/sdk-react', () => sdk);

import {
  bindTelegramBackButton,
  bindTelegramMainButton,
  bindTelegramSecondaryButton,
  downloadTelegramAsset,
  getRawInitData,
  getTelegramClosingConfirmationState,
  getTelegramDeviceStorageItem,
  getTelegramMiniAppCapabilities,
  initTelegram,
  requestTelegramChat,
  requestTelegramFullscreen,
  shareTelegramPreparedMessage,
} from './telegram';

describe('Telegram bridge SSR/non-Telegram fallback', () => {
  it('does not crash or initialize Telegram when window is absent', async () => {
    expect(typeof window).toBe('undefined');
    expect(() => getRawInitData()).not.toThrow();
    expect(typeof getRawInitData()).toBe('string');

    expect(getTelegramMiniAppCapabilities()).toEqual({
      version: null,
      backButton: false,
      hapticFeedback: false,
      closingConfirmation: false,
      secondaryButton: false,
      fullscreen: false,
      safeArea: false,
      contentSafeArea: false,
      sharePreparedMessage: false,
      downloadFile: false,
      deviceStorage: false,
      secureStorage: false,
      requestChat: false,
    });

    await expect(initTelegram()).resolves.toBeUndefined();
    expect(sdk.setDebug).not.toHaveBeenCalled();
    expect(sdk.init).not.toHaveBeenCalled();
    expect(sdk.miniApp.mount).not.toHaveBeenCalled();
    expect(sdk.viewport.mount).not.toHaveBeenCalled();
  });

  it('fails optional native actions safely without touching their providers', async () => {
    await expect(requestTelegramFullscreen()).resolves.toBe('unavailable');
    expect(getTelegramClosingConfirmationState()).toBeNull();
    expect(bindTelegramBackButton(vi.fn())).toBeNull();
    expect(bindTelegramMainButton('Continue', vi.fn())).toBeNull();
    expect(bindTelegramSecondaryButton('Cancel', vi.fn())).toBeNull();

    await expect(getTelegramDeviceStorageItem('preview-mode')).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
    await expect(requestTelegramChat('prepared-request')).resolves.toEqual({
      ok: false,
      status: 'unsupported',
    });
    await expect(shareTelegramPreparedMessage('prepared-message')).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
    await expect(
      downloadTelegramAsset('https://example.com/export.zip', 'export.zip'),
    ).resolves.toEqual({ ok: false, reason: 'unsupported' });

    expect(sdk.deviceStorage.getItem).not.toHaveBeenCalled();
    expect(sdk.shareMessage).not.toHaveBeenCalled();
    expect(sdk.downloadFile).not.toHaveBeenCalled();
    expect(sdk.backButton.mount).not.toHaveBeenCalled();
    expect(sdk.mainButton.mount).not.toHaveBeenCalled();
    expect(sdk.secondaryButton.mount).not.toHaveBeenCalled();
  });
});
