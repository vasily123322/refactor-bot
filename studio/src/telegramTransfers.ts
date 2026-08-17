export type TelegramTransferError = Readonly<{
  name: string;
  message: string;
}>;

export type TelegramTransferResult =
  | Readonly<{ ok: true }>
  | Readonly<{ ok: false; reason: 'unsupported' | 'invalid-input' }>
  | Readonly<{
      ok: false;
      reason: 'provider-error';
      error: TelegramTransferError;
    }>;

type AvailableShareMessage = ((preparedMessageId: string) => Promise<void>) & {
  isAvailable(): boolean;
};

type AvailableDownloadFile = ((url: string, fileName: string) => Promise<void>) & {
  isAvailable(): boolean;
};

export type TelegramTransferBridgeDependencies = Readonly<{
  isShareMessageSupported(): boolean;
  isDownloadFileSupported(): boolean;
  shareMessage?: AvailableShareMessage;
  downloadFile?: AvailableDownloadFile;
}>;

export function normalizeTelegramTransferError(error: unknown): TelegramTransferError {
  if (error instanceof Error) {
    return {
      name: error.name || 'Error',
      message: error.message || 'Unknown error',
    };
  }
  return { name: 'Error', message: String(error) };
}

function validHttpsUrl(value: string): boolean {
  try {
    return new URL(value).protocol === 'https:';
  } catch {
    return false;
  }
}

export function createTelegramTransferBridge(
  deps: TelegramTransferBridgeDependencies,
) {
  async function sharePreparedMessage(
    preparedMessageId: string,
  ): Promise<TelegramTransferResult> {
    const normalizedId = preparedMessageId.trim();
    if (!normalizedId) return { ok: false, reason: 'invalid-input' };
    const provider = deps.shareMessage;
    if (
      !deps.isShareMessageSupported() ||
      !provider ||
      !provider.isAvailable()
    ) {
      return { ok: false, reason: 'unsupported' };
    }
    try {
      await provider(normalizedId);
      return { ok: true };
    } catch (error) {
      return {
        ok: false,
        reason: 'provider-error',
        error: normalizeTelegramTransferError(error),
      };
    }
  }

  async function downloadAsset(
    url: string,
    fileName: string,
  ): Promise<TelegramTransferResult> {
    const normalizedUrl = url.trim();
    const normalizedFileName = fileName.trim();
    if (!normalizedFileName || !validHttpsUrl(normalizedUrl)) {
      return { ok: false, reason: 'invalid-input' };
    }
    const provider = deps.downloadFile;
    if (
      !deps.isDownloadFileSupported() ||
      !provider ||
      !provider.isAvailable()
    ) {
      return { ok: false, reason: 'unsupported' };
    }
    try {
      await provider(normalizedUrl, normalizedFileName);
      return { ok: true };
    } catch (error) {
      return {
        ok: false,
        reason: 'provider-error',
        error: normalizeTelegramTransferError(error),
      };
    }
  }

  return Object.freeze({ sharePreparedMessage, downloadAsset });
}
