import {
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