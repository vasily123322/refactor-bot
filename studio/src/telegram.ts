import {
  init as initSDK,
  miniApp,
  setDebug,
  themeParams,
  viewport,
} from '@tma.js/sdk-react';

function rawLaunchParam(name: string): string | null {
  for (const value of [window.location.search, window.location.hash]) {
    const params = new URLSearchParams(value.replace(/^[?#]/, ''));
    const found = params.get(name);
    if (found) return found;
  }
  return null;
}

export function getRawInitData(): string {
  return rawLaunchParam('tgWebAppData') ?? import.meta.env.VITE_DEV_INIT_DATA ?? '';
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
