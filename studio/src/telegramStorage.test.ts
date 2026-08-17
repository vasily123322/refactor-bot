import { describe, expect, it, vi } from 'vitest';

import {
  STUDIO_DEVICE_STORAGE_KEYS,
  createTelegramStorageBridge,
} from './telegramStorage';

function availableAsync<Args extends unknown[], Value>(
  implementation: (...args: Args) => Promise<Value>,
  available = true,
) {
  return Object.assign(vi.fn(implementation), {
    isAvailable: vi.fn(() => available),
  });
}

function deviceProvider(available = true) {
  return {
    getItem: availableAsync(async (_key: string) => 'value', available),
    setItem: availableAsync(async (_key: string, _value: string | null) => undefined, available),
    deleteItem: availableAsync(async (_key: string) => undefined, available),
  };
}

function secureProvider(available = true) {
  return {
    getItem: availableAsync(
      async (_key: string) => ({ value: 'secret-value', canRestore: false }),
      available,
    ),
    setItem: availableAsync(async (_key: string, _value: string | null) => undefined, available),
    deleteItem: availableAsync(async (_key: string) => undefined, available),
    clear: availableAsync(async () => undefined, available),
    restoreItem: availableAsync(async (_key: string) => 'restored-value', available),
  };
}

describe('Telegram storage bridge', () => {
  it('fails closed below the Telegram 9.0 capability boundary', async () => {
    const deviceStorage = deviceProvider();
    const secureStorage = secureProvider();
    const bridge = createTelegramStorageBridge({
      isDeviceStorageSupported: () => false,
      isSecureStorageSupported: () => false,
      deviceStorage,
      secureStorage,
    });

    await expect(bridge.deviceGet('preview-mode')).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
    await expect(bridge.secureGet('explicit-key')).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
    expect(deviceStorage.getItem).not.toHaveBeenCalled();
    expect(secureStorage.getItem).not.toHaveBeenCalled();
  });

  it('fails safely when the client is supported but provider methods are unavailable', async () => {
    const bridge = createTelegramStorageBridge({
      isDeviceStorageSupported: () => true,
      isSecureStorageSupported: () => true,
      deviceStorage: deviceProvider(false),
      secureStorage: secureProvider(false),
    });

    await expect(bridge.deviceSet('filters', '{}')).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
    await expect(bridge.secureSet('explicit-key', 'value')).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
  });

  it('uses only the non-authoritative Studio UX namespace for DeviceStorage', async () => {
    const deviceStorage = deviceProvider();
    const bridge = createTelegramStorageBridge({
      isDeviceStorageSupported: () => true,
      isSecureStorageSupported: () => false,
      deviceStorage,
    });

    await expect(bridge.deviceSet('last-active-section', 'planner')).resolves.toEqual({
      ok: true,
      value: undefined,
    });
    expect(deviceStorage.setItem).toHaveBeenCalledWith(
      'studio:ux:last-active-section',
      'planner',
    );

    await expect(bridge.deviceGet('preview-mode')).resolves.toEqual({
      ok: true,
      value: 'value',
    });
    expect(deviceStorage.getItem).toHaveBeenCalledWith('studio:ux:preview-mode');
  });

  it('clears only allowlisted DeviceStorage UX keys', async () => {
    const deviceStorage = deviceProvider();
    const bridge = createTelegramStorageBridge({
      isDeviceStorageSupported: () => true,
      isSecureStorageSupported: () => false,
      deviceStorage,
    });

    await expect(bridge.deviceClearUxState()).resolves.toEqual({
      ok: true,
      value: undefined,
    });
    expect(deviceStorage.deleteItem).toHaveBeenCalledTimes(
      STUDIO_DEVICE_STORAGE_KEYS.length,
    );
    for (const key of STUDIO_DEVICE_STORAGE_KEYS) {
      expect(deviceStorage.deleteItem).toHaveBeenCalledWith(`studio:ux:${key}`);
    }
  });

  it('returns SecureStorage values without assigning them auth/session authority', async () => {
    const secureStorage = secureProvider();
    const bridge = createTelegramStorageBridge({
      isDeviceStorageSupported: () => false,
      isSecureStorageSupported: () => true,
      secureStorage,
    });

    await expect(bridge.secureGet('caller-owned-key')).resolves.toEqual({
      ok: true,
      value: { value: 'secret-value', canRestore: false },
    });
    await expect(bridge.secureRestore('caller-owned-key')).resolves.toEqual({
      ok: true,
      value: 'restored-value',
    });
    expect(secureStorage.getItem).toHaveBeenCalledWith('caller-owned-key');
    expect(secureStorage.restoreItem).toHaveBeenCalledWith('caller-owned-key');
  });

  it('surfaces provider rejection as an explicit provider error', async () => {
    const deviceStorage = deviceProvider();
    deviceStorage.getItem.mockRejectedValueOnce(new Error('provider denied request'));
    const bridge = createTelegramStorageBridge({
      isDeviceStorageSupported: () => true,
      isSecureStorageSupported: () => false,
      deviceStorage,
    });

    await expect(bridge.deviceGet('workspace-hint')).resolves.toEqual({
      ok: false,
      reason: 'provider-error',
      error: { name: 'Error', message: 'provider denied request' },
    });
  });

  it('does not read or write storage during construction and tolerates provider absence', async () => {
    const deviceStorage = deviceProvider();
    const secureStorage = secureProvider();
    createTelegramStorageBridge({
      isDeviceStorageSupported: () => true,
      isSecureStorageSupported: () => true,
      deviceStorage,
      secureStorage,
    });
    expect(deviceStorage.getItem).not.toHaveBeenCalled();
    expect(deviceStorage.setItem).not.toHaveBeenCalled();
    expect(secureStorage.getItem).not.toHaveBeenCalled();
    expect(secureStorage.setItem).not.toHaveBeenCalled();

    const absent = createTelegramStorageBridge({
      isDeviceStorageSupported: () => true,
      isSecureStorageSupported: () => true,
    });
    await expect(absent.deviceGet('draft-recovery-pointer')).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
    await expect(absent.secureClear()).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
  });
});
