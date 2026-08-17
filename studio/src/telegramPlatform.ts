export type TelegramMiniAppCapabilities = Readonly<{
  version: string | null;
  fullscreen: boolean;
  safeArea: boolean;
  contentSafeArea: boolean;
  deviceStorage: boolean;
  secureStorage: boolean;
  requestChat: boolean;
}>;

function versionParts(value: string | null | undefined): number[] | null {
  if (typeof value !== 'string') return null;
  const normalized = value.trim();
  if (!/^\d+(?:\.\d+)*$/.test(normalized)) return null;
  return normalized.split('.').map((part) => Number.parseInt(part, 10));
}

export function normalizeTelegramMiniAppVersion(
  value: string | null | undefined,
): string | null {
  const parts = versionParts(value);
  return parts ? parts.join('.') : null;
}

export function compareTelegramMiniAppVersions(
  current: string | null | undefined,
  required: string | null | undefined,
): -1 | 0 | 1 | null {
  const currentParts = versionParts(current);
  const requiredParts = versionParts(required);
  if (!currentParts || !requiredParts) return null;

  const width = Math.max(currentParts.length, requiredParts.length);
  for (let index = 0; index < width; index += 1) {
    const currentPart = currentParts[index] ?? 0;
    const requiredPart = requiredParts[index] ?? 0;
    if (currentPart > requiredPart) return 1;
    if (currentPart < requiredPart) return -1;
  }
  return 0;
}

export function isTelegramMiniAppVersionAtLeast(
  current: string | null | undefined,
  required: string,
): boolean {
  const compared = compareTelegramMiniAppVersions(current, required);
  return compared !== null && compared >= 0;
}

export function telegramCapabilitiesForVersion(
  version: string | null | undefined,
): TelegramMiniAppCapabilities {
  const normalized = normalizeTelegramMiniAppVersion(version);
  const atLeast8 = isTelegramMiniAppVersionAtLeast(normalized, '8.0');
  const atLeast9 = isTelegramMiniAppVersionAtLeast(normalized, '9.0');

  return Object.freeze({
    version: normalized,
    fullscreen: atLeast8,
    safeArea: atLeast8,
    contentSafeArea: atLeast8,
    deviceStorage: atLeast9,
    secureStorage: atLeast9,
    requestChat: isTelegramMiniAppVersionAtLeast(normalized, '9.6'),
  });
}

export function readTelegramLaunchParam(
  search: string,
  hash: string,
  name: string,
): string | null {
  for (const value of [search, hash]) {
    const params = new URLSearchParams(value.replace(/^[?#]/, ''));
    const found = params.get(name);
    if (found) return found;
  }
  return null;
}