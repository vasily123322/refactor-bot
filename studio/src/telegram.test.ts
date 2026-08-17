import { describe, expect, it } from 'vitest';

import {
  compareTelegramMiniAppVersions,
  isTelegramMiniAppVersionAtLeast,
  normalizeTelegramMiniAppVersion,
  readTelegramLaunchParam,
  telegramCapabilitiesForVersion,
} from './telegramPlatform';

describe('Telegram Mini App version foundation', () => {
  it('compares numeric version segments rather than lexicographic strings', () => {
    expect(compareTelegramMiniAppVersions('9.10', '9.6')).toBe(1);
    expect(compareTelegramMiniAppVersions('9.6', '9.6.0')).toBe(0);
    expect(compareTelegramMiniAppVersions('8.10', '9.0')).toBe(-1);
  });

  it('fails closed for malformed or missing versions', () => {
    expect(normalizeTelegramMiniAppVersion(' 9.6 ')).toBe('9.6');
    expect(normalizeTelegramMiniAppVersion('9.x')).toBeNull();
    expect(compareTelegramMiniAppVersions('9.x', '9.0')).toBeNull();
    expect(isTelegramMiniAppVersionAtLeast(null, '8.0')).toBe(false);
    expect(isTelegramMiniAppVersionAtLeast('unknown', '8.0')).toBe(false);
  });

  it('gates the verified lifecycle and platform capability boundaries', () => {
    expect(telegramCapabilitiesForVersion('6.0')).toMatchObject({
      backButton: false,
      hapticFeedback: false,
      closingConfirmation: false,
      secondaryButton: false,
    });
    expect(telegramCapabilitiesForVersion('6.1')).toMatchObject({
      backButton: true,
      hapticFeedback: true,
      closingConfirmation: false,
      secondaryButton: false,
    });
    expect(telegramCapabilitiesForVersion('6.2')).toMatchObject({
      backButton: true,
      hapticFeedback: true,
      closingConfirmation: true,
      secondaryButton: false,
    });
    expect(telegramCapabilitiesForVersion('7.10')).toMatchObject({
      backButton: true,
      hapticFeedback: true,
      closingConfirmation: true,
      secondaryButton: true,
      fullscreen: false,
      safeArea: false,
      contentSafeArea: false,
      sharePreparedMessage: false,
      downloadFile: false,
      deviceStorage: false,
      secureStorage: false,
      requestChat: false,
    });
    expect(telegramCapabilitiesForVersion('8.0')).toMatchObject({
      fullscreen: true,
      safeArea: true,
      contentSafeArea: true,
      sharePreparedMessage: true,
      downloadFile: true,
      deviceStorage: false,
      secureStorage: false,
      requestChat: false,
    });
    expect(telegramCapabilitiesForVersion('9.0')).toMatchObject({
      fullscreen: true,
      safeArea: true,
      contentSafeArea: true,
      sharePreparedMessage: true,
      downloadFile: true,
      deviceStorage: true,
      secureStorage: true,
      requestChat: false,
    });
    expect(telegramCapabilitiesForVersion('9.6')).toMatchObject({
      sharePreparedMessage: true,
      downloadFile: true,
      deviceStorage: true,
      secureStorage: true,
      requestChat: true,
    });
  });

  it('returns no capabilities when Telegram does not provide a usable version', () => {
    expect(telegramCapabilitiesForVersion(null)).toEqual({
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
  });
});

describe('Telegram launch parameters', () => {
  it('reads query launch parameters before hash parameters', () => {
    expect(
      readTelegramLaunchParam(
        '?tgWebAppVersion=9.6&other=1',
        '#tgWebAppVersion=8.0',
        'tgWebAppVersion',
      ),
    ).toBe('9.6');
  });

  it('falls back to Telegram hash launch parameters', () => {
    expect(
      readTelegramLaunchParam('', '#tgWebAppData=signed%3Dvalue', 'tgWebAppData'),
    ).toBe('signed=value');
  });

  it('returns null when a launch parameter is absent', () => {
    expect(readTelegramLaunchParam('?foo=bar', '#baz=qux', 'tgWebAppData')).toBeNull();
  });
});