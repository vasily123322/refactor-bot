import {
  closingBehavior,
  hapticFeedback,
  init as initSDK,
  miniApp,
  setDebug,
  themeParams,
  viewport,
} from '@tma.js/sdk-react';

import {
  normalizeTelegramMiniAppVersion,
  readTelegramLaunchParam,
  telegramCapabilitiesForVersion,
} from './telegramPlatform';
import type { TelegramMiniAppCapabilities } from './telegramPlatform';

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

function rawLaunchParam(name: string): string | null {
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

export async function initTelegram(): Promise<void> {
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