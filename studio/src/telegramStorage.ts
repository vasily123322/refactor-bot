export type TelegramStorageError = Readonly<{
  name: string;
  message: string;
}>;

export type TelegramStorageResult<T> =
  | Readonly<{ ok: true; value: T }>
  | Readonly<{
      ok: false;
      reason: 'unsupported' | 'provider-error';
      error?: TelegramStorageError;
    }>;

export type StudioDeviceStorageKey =
  | 'last-active-section'
  | 'selected-channel-hint'
  | 'workspace-hint'
  | 'editor-ui-preferences'
  | 'collapsed-panels'
  | 'filters'
  | 'preview-mode'
  | 'draft-recovery-pointer';

export const STUDIO_DEVICE_STORAGE_KEYS: readonly StudioDeviceStorageKey[] = Object.freeze([
  'last-active-section',
  'selected-channel-hint',
  'workspace-hint',
  'editor-ui-preferences',
  'collapsed-panels',
  'filters',
  'preview-mode',
  'draft-recovery-pointer',
]);

type AvailableAsync<Args extends unknown[], Value> = ((...args: Args) => Promise<Value>) & {
  isAvailable(): boolean;
};

type DeviceStorageProvider = Readonly<{
  getItem: AvailableAsync<[key: string], string | null>;
  setItem: AvailableAsync<[key: string, value: string | null], void>;
  deleteItem: AvailableAsync<[key: string], void>;
}>;

type SecureStorageProvider = Readonly<{
  getItem: AvailableAsync<
    [key: string],
    { value: string | null; canRestore: boolean }
  >;
  setItem: AvailableAsync<[key: string, value: string | null], void>;
  deleteItem: AvailableAsync<[key: string], void>;
  clear: AvailableAsync<[], void>;
  restoreItem: AvailableAsync<[key: string], string | null>;
}>;

export type TelegramStorageBridgeDependencies = Readonly<{
  isDeviceStorageSupported(): boolean;
  isSecureStorageSupported(): boolean;
  deviceStorage?: DeviceStorageProvider;
  secureStorage?: SecureStorageProvider;
}>;

function providerError(error: unknown): TelegramStorageResult<never> {
  if (error instanceof Error) {
    return {
      ok: false,
      reason: 'provider-error',
      error: { name: error.name || 'Error', message: error.message || 'Unknown error' },
    };
  }
  return {
    ok: false,
    reason: 'provider-error',
    error: { name: 'Error', message: String(error) },
  };
}

function unavailable<T>(): TelegramStorageResult<T> {
  return { ok: false, reason: 'unsupported' };
}

function deviceStorageKey(key: StudioDeviceStorageKey): string {
  // DeviceStorage is intentionally scoped to non-authoritative Studio UX state.
  return `studio:ux:${key}`;
}

function secureKeyIsUsable(key: string): boolean {
  return key.trim().length > 0;
}

export function createTelegramStorageBridge(deps: TelegramStorageBridgeDependencies) {
  async function deviceGet(
    key: StudioDeviceStorageKey,
  ): Promise<TelegramStorageResult<string | null>> {
    const provider = deps.deviceStorage;
    if (
      !deps.isDeviceStorageSupported() ||
      !provider ||
      !provider.getItem.isAvailable()
    ) {
      return unavailable();
    }
    try {
      return { ok: true, value: await provider.getItem(deviceStorageKey(key)) };
    } catch (error) {
      return providerError(error);
    }
  }

  async function deviceSet(
    key: StudioDeviceStorageKey,
    value: string,
  ): Promise<TelegramStorageResult<void>> {
    const provider = deps.deviceStorage;
    if (
      !deps.isDeviceStorageSupported() ||
      !provider ||
      !provider.setItem.isAvailable()
    ) {
      return unavailable();
    }
    try {
      await provider.setItem(deviceStorageKey(key), value);
      return { ok: true, value: undefined };
    } catch (error) {
      return providerError(error);
    }
  }

  async function deviceRemove(
    key: StudioDeviceStorageKey,
  ): Promise<TelegramStorageResult<void>> {
    const provider = deps.deviceStorage;
    if (
      !deps.isDeviceStorageSupported() ||
      !provider ||
      !provider.deleteItem.isAvailable()
    ) {
      return unavailable();
    }
    try {
      await provider.deleteItem(deviceStorageKey(key));
      return { ok: true, value: undefined };
    } catch (error) {
      return providerError(error);
    }
  }

  async function deviceClearUxState(): Promise<TelegramStorageResult<void>> {
    const provider = deps.deviceStorage;
    if (
      !deps.isDeviceStorageSupported() ||
      !provider ||
      !provider.deleteItem.isAvailable()
    ) {
      return unavailable();
    }
    try {
      await Promise.all(
        STUDIO_DEVICE_STORAGE_KEYS.map((key) =>
          provider.deleteItem(deviceStorageKey(key)),
        ),
      );
      return { ok: true, value: undefined };
    } catch (error) {
      return providerError(error);
    }
  }

  async function secureGet(
    key: string,
  ): Promise<
    TelegramStorageResult<{ value: string | null; canRestore: boolean }>
  > {
    const provider = deps.secureStorage;
    if (
      !secureKeyIsUsable(key) ||
      !deps.isSecureStorageSupported() ||
      !provider ||
      !provider.getItem.isAvailable()
    ) {
      return unavailable();
    }
    try {
      return { ok: true, value: await provider.getItem(key) };
    } catch (error) {
      return providerError(error);
    }
  }

  async function secureSet(
    key: string,
    value: string,
  ): Promise<TelegramStorageResult<void>> {
    const provider = deps.secureStorage;
    if (
      !secureKeyIsUsable(key) ||
      !deps.isSecureStorageSupported() ||
      !provider ||
      !provider.setItem.isAvailable()
    ) {
      return unavailable();
    }
    try {
      await provider.setItem(key, value);
      return { ok: true, value: undefined };
    } catch (error) {
      return providerError(error);
    }
  }

  async function secureRemove(key: string): Promise<TelegramStorageResult<void>> {
    const provider = deps.secureStorage;
    if (
      !secureKeyIsUsable(key) ||
      !deps.isSecureStorageSupported() ||
      !provider ||
      !provider.deleteItem.isAvailable()
    ) {
      return unavailable();
    }
    try {
      await provider.deleteItem(key);
      return { ok: true, value: undefined };
    } catch (error) {
      return providerError(error);
    }
  }

  async function secureClear(): Promise<TelegramStorageResult<void>> {
    const provider = deps.secureStorage;
    if (
      !deps.isSecureStorageSupported() ||
      !provider ||
      !provider.clear.isAvailable()
    ) {
      return unavailable();
    }
    try {
      await provider.clear();
      return { ok: true, value: undefined };
    } catch (error) {
      return providerError(error);
    }
  }

  async function secureRestore(
    key: string,
  ): Promise<TelegramStorageResult<string | null>> {
    const provider = deps.secureStorage;
    if (
      !secureKeyIsUsable(key) ||
      !deps.isSecureStorageSupported() ||
      !provider ||
      !provider.restoreItem.isAvailable()
    ) {
      return unavailable();
    }
    try {
      return { ok: true, value: await provider.restoreItem(key) };
    } catch (error) {
      return providerError(error);
    }
  }

  return Object.freeze({
    deviceGet,
    deviceSet,
    deviceRemove,
    deviceClearUxState,
    secureGet,
    secureSet,
    secureRemove,
    secureClear,
    secureRestore,
  });
}
