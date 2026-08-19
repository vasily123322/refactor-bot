import {
  backButton,
  closingBehavior,
  deviceStorage,
  downloadFile,
  hapticFeedback,
  init as initSDK,
  mainButton,
  miniApp,
  secondaryButton,
  secureStorage,
  setDebug,
  shareMessage,
  themeParams,
  viewport,
} from '@tma.js/sdk-react';

import { createTelegramButtonBridge } from './telegramButtons';
export type { TelegramSecondaryButtonPosition } from './telegramButtons';
import {
  normalizeTelegramMiniAppVersion,
  readTelegramLaunchParam,
  telegramCapabilitiesForVersion,
} from './telegramPlatform';
import type { TelegramMiniAppCapabilities } from './telegramPlatform';
import { createTelegramRequestChatBridge } from './telegramRequestChat';
export type {
  TelegramRequestChatError,
  TelegramRequestChatResult,
} from './telegramRequestChat';
import { createTelegramStorageBridge } from './telegramStorage';
export type {
  StudioDeviceStorageKey,
  TelegramStorageError,
  TelegramStorageResult,
} from './telegramStorage';
import { createTelegramTransferBridge } from './telegramTransfers';
export type {
  TelegramTransferError,
  TelegramTransferResult,
} from './telegramTransfers';

export type TelegramFullscreenActionResult =
  | 'requested'
  | 'exited'
  | 'already-fullscreen'
  | 'already-windowed'
  | 'unavailable';

export type TelegramHapticImpactStyle =
  | 'light'
  | 'medium'
  | 'heavy'
  | 'rigid'
  | 'soft';
export type TelegramHapticNotificationType = 'error' | 'success' | 'warning';

type OfficialTelegramRequestChatWebApp = Readonly<{
  requestChat?: (
    preparedRequestId: string,
    callback?: (sent: boolean) => void,
  ) => void;
}>;

function rawLaunchParam(name: string): string | null {
  if (typeof window === 'undefined') return null;
  return readTelegramLaunchParam(window.location.search, window.location.hash, name);
}

export function getRawInitData(): string {
  const launchInitData = rawLaunchParam('tgWebAppData');
  if (launchInitData) return launchInitData;

  // Never ship a development credential fallback into a production build. The
  // server remains the authority and validates raw Telegram initData on every request.
  return import.meta.env.DEV ? import.meta.env.VITE_DEV_INIT_DATA ?? '' : '';
}

export function getTelegramMiniAppVersion(): string | null {
  return normalizeTelegramMiniAppVersion(rawLaunchParam('tgWebAppVersion'));
}

export function getTelegramMiniAppCapabilities(): TelegramMiniAppCapabilities {
  return telegramCapabilitiesForVersion(getTelegramMiniAppVersion());
}

const telegramStorage = createTelegramStorageBridge({
  isDeviceStorageSupported: () => getTelegramMiniAppCapabilities().deviceStorage,
  isSecureStorageSupported: () => getTelegramMiniAppCapabilities().secureStorage,
  deviceStorage,
  secureStorage,
});

export const getTelegramDeviceStorageItem = telegramStorage.deviceGet;
export const setTelegramDeviceStorageItem = telegramStorage.deviceSet;
export const removeTelegramDeviceStorageItem = telegramStorage.deviceRemove;
export const clearTelegramDeviceStorageUxState = telegramStorage.deviceClearUxState;
export const getTelegramSecureStorageItem = telegramStorage.secureGet;
export const setTelegramSecureStorageItem = telegramStorage.secureSet;
export const removeTelegramSecureStorageItem = telegramStorage.secureRemove;
export const clearTelegramSecureStorage = telegramStorage.secureClear;
export const restoreTelegramSecureStorageItem = telegramStorage.secureRestore;

function getOfficialRequestChatProvider() {
  if (typeof window === 'undefined') return undefined;
  const telegramWindow = window as typeof window & {
    Telegram?: { WebApp?: OfficialTelegramRequestChatWebApp };
  };
  const webApp = telegramWindow.Telegram?.WebApp;
  if (!webApp || typeof webApp.requestChat !== 'function') return undefined;
  return {
    requestChat(preparedRequestId: string, callback: (sent: boolean) => void) {
      webApp.requestChat!(preparedRequestId, callback);
    },
  };
}

const requestChatBridge = createTelegramRequestChatBridge({
  isSupported: () => getTelegramMiniAppCapabilities().requestChat,
  getProvider: getOfficialRequestChatProvider,
});

export const requestTelegramChat = requestChatBridge;

const telegramTransfers = createTelegramTransferBridge({
  isShareMessageSupported: () =>
    getTelegramMiniAppCapabilities().sharePreparedMessage,
  isDownloadFileSupported: () => getTelegramMiniAppCapabilities().downloadFile,
  shareMessage,
  downloadFile,
});

export const shareTelegramPreparedMessage = telegramTransfers.sharePreparedMessage;
export const downloadTelegramAsset = telegramTransfers.downloadAsset;

function fullscreenBridgeReady(): boolean {
  return Boolean(
    getTelegramMiniAppCapabilities().fullscreen &&
      viewport.isMounted() &&
      viewport.requestFullscreen.isAvailable() &&
      viewport.exitFullscreen.isAvailable(),
  );
}

export function getTelegramFullscreenState(): boolean | null {
  return fullscreenBridgeReady() ? viewport.isFullscreen() : null;
}

export async function requestTelegramFullscreen(): Promise<TelegramFullscreenActionResult> {
  if (!fullscreenBridgeReady()) return 'unavailable';
  if (viewport.isFullscreen()) return 'already-fullscreen';
  await viewport.requestFullscreen();
  return 'requested';
}

export async function exitTelegramFullscreen(): Promise<TelegramFullscreenActionResult> {
  if (!fullscreenBridgeReady()) return 'unavailable';
  if (!viewport.isFullscreen()) return 'already-windowed';
  await viewport.exitFullscreen();
  return 'exited';
}

function ensureClosingBehaviorReady(): boolean {
  if (
    !getTelegramMiniAppCapabilities().closingConfirmation ||
    !closingBehavior.mount.isAvailable()
  ) {
    return false;
  }
  if (!closingBehavior.isMounted()) closingBehavior.mount();
  return closingBehavior.isMounted();
}

export function getTelegramClosingConfirmationState(): boolean | null {
  return ensureClosingBehaviorReady()
    ? closingBehavior.isConfirmationEnabled()
    : null;
}

export function setTelegramClosingConfirmation(enabled: boolean): boolean {
  if (!ensureClosingBehaviorReady()) return false;
  const action = enabled
    ? closingBehavior.enableConfirmation
    : closingBehavior.disableConfirmation;
  if (!action.isAvailable()) return false;
  action();
  return true;
}

export function disposeTelegramClosingConfirmation(): boolean {
  if (!closingBehavior.isMounted()) return true;
  if (closingBehavior.isConfirmationEnabled()) {
    if (!closingBehavior.disableConfirmation.isAvailable()) return false;
    closingBehavior.disableConfirmation();
  }
  closingBehavior.unmount();
  return true;
}

export function triggerTelegramHapticImpact(
  style: TelegramHapticImpactStyle,
): boolean {
  if (
    !getTelegramMiniAppCapabilities().hapticFeedback ||
    !hapticFeedback.impactOccurred.isAvailable()
  ) {
    return false;
  }
  hapticFeedback.impactOccurred(style);
  return true;
}

export function triggerTelegramHapticNotification(
  type: TelegramHapticNotificationType,
): boolean {
  if (
    !getTelegramMiniAppCapabilities().hapticFeedback ||
    !hapticFeedback.notificationOccurred.isAvailable()
  ) {
    return false;
  }
  hapticFeedback.notificationOccurred(type);
  return true;
}

export function triggerTelegramHapticSelectionChanged(): boolean {
  if (
    !getTelegramMiniAppCapabilities().hapticFeedback ||
    !hapticFeedback.selectionChanged.isAvailable()
  ) {
    return false;
  }
  hapticFeedback.selectionChanged();
  return true;
}

const telegramButtons = createTelegramButtonBridge({
  isBackButtonSupported: () => getTelegramMiniAppCapabilities().backButton,
  isSecondaryButtonSupported: () =>
    getTelegramMiniAppCapabilities().secondaryButton,
  backButton,
  mainButton,
  secondaryButton,
});

export const bindTelegramBackButton = telegramButtons.bindBack;
export const bindTelegramMainButton = telegramButtons.bindMain;
export const bindTelegramSecondaryButton = telegramButtons.bindSecondary;
export const disposeTelegramNativeButtons = telegramButtons.disposeAll;

export async function initTelegram(): Promise<void> {
  if (typeof window === 'undefined') return;

  setDebug(import.meta.env.DEV);
  initSDK();

  if (miniApp.mount.isAvailable()) {
    themeParams.mount();
    miniApp.mount();
    themeParams.bindCssVars();
  }
  if (viewport.mount.isAvailable()) {
    await viewport.mount();
    viewport.bindCssVars();
  }
}