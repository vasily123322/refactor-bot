import { describe, expect, it, vi } from 'vitest';

import { createTelegramTransferBridge } from './telegramTransfers';

function availableAsync<Args extends unknown[]>(
  implementation: (...args: Args) => Promise<void>,
  available = true,
) {
  return Object.assign(vi.fn(implementation), {
    isAvailable: vi.fn(() => available),
  });
}

describe('Telegram share/download bridge', () => {
  it('fails closed below the Telegram 8.0 capability boundary', async () => {
    const shareMessage = availableAsync(async (_id: string) => undefined);
    const downloadFile = availableAsync(
      async (_url: string, _fileName: string) => undefined,
    );
    const bridge = createTelegramTransferBridge({
      isShareMessageSupported: () => false,
      isDownloadFileSupported: () => false,
      shareMessage,
      downloadFile,
    });

    await expect(bridge.sharePreparedMessage('prepared-id')).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
    await expect(
      bridge.downloadAsset('https://example.com/export.zip', 'export.zip'),
    ).resolves.toEqual({ ok: false, reason: 'unsupported' });
    expect(shareMessage).not.toHaveBeenCalled();
    expect(downloadFile).not.toHaveBeenCalled();
  });

  it('fails safely when SDK providers are unavailable', async () => {
    const bridge = createTelegramTransferBridge({
      isShareMessageSupported: () => true,
      isDownloadFileSupported: () => true,
      shareMessage: availableAsync(async (_id: string) => undefined, false),
      downloadFile: availableAsync(
        async (_url: string, _fileName: string) => undefined,
        false,
      ),
    });

    await expect(bridge.sharePreparedMessage('prepared-id')).resolves.toEqual({
      ok: false,
      reason: 'unsupported',
    });
    await expect(
      bridge.downloadAsset('https://example.com/export.zip', 'export.zip'),
    ).resolves.toEqual({ ok: false, reason: 'unsupported' });
  });

  it('invokes native share/download only after an explicit caller action', async () => {
    const shareMessage = availableAsync(async (_id: string) => undefined);
    const downloadFile = availableAsync(
      async (_url: string, _fileName: string) => undefined,
    );
    const bridge = createTelegramTransferBridge({
      isShareMessageSupported: () => true,
      isDownloadFileSupported: () => true,
      shareMessage,
      downloadFile,
    });

    expect(shareMessage).not.toHaveBeenCalled();
    expect(downloadFile).not.toHaveBeenCalled();

    await expect(bridge.sharePreparedMessage('  prepared-id  ')).resolves.toEqual({
      ok: true,
    });
    expect(shareMessage).toHaveBeenCalledWith('prepared-id');

    await expect(
      bridge.downloadAsset(' https://example.com/export.zip ', ' export.zip '),
    ).resolves.toEqual({ ok: true });
    expect(downloadFile).toHaveBeenCalledWith(
      'https://example.com/export.zip',
      'export.zip',
    );
  });

  it('rejects invalid prepared ids, non-HTTPS URLs and empty file names locally', async () => {
    const shareMessage = availableAsync(async (_id: string) => undefined);
    const downloadFile = availableAsync(
      async (_url: string, _fileName: string) => undefined,
    );
    const bridge = createTelegramTransferBridge({
      isShareMessageSupported: () => true,
      isDownloadFileSupported: () => true,
      shareMessage,
      downloadFile,
    });

    await expect(bridge.sharePreparedMessage('   ')).resolves.toEqual({
      ok: false,
      reason: 'invalid-input',
    });
    await expect(
      bridge.downloadAsset('http://example.com/export.zip', 'export.zip'),
    ).resolves.toEqual({ ok: false, reason: 'invalid-input' });
    await expect(
      bridge.downloadAsset('https://example.com/export.zip', '   '),
    ).resolves.toEqual({ ok: false, reason: 'invalid-input' });
    expect(shareMessage).not.toHaveBeenCalled();
    expect(downloadFile).not.toHaveBeenCalled();
  });

  it('surfaces share and download provider rejection explicitly', async () => {
    const shareMessage = availableAsync(async (_id: string) => {
      throw new Error('share rejected');
    });
    const downloadFile = availableAsync(async (_url: string, _fileName: string) => {
      const error = new Error('user denied download');
      error.name = 'AccessDeniedError';
      throw error;
    });
    const bridge = createTelegramTransferBridge({
      isShareMessageSupported: () => true,
      isDownloadFileSupported: () => true,
      shareMessage,
      downloadFile,
    });

    await expect(bridge.sharePreparedMessage('prepared-id')).resolves.toEqual({
      ok: false,
      reason: 'provider-error',
      error: { name: 'Error', message: 'share rejected' },
    });
    await expect(
      bridge.downloadAsset('https://example.com/export.zip', 'export.zip'),
    ).resolves.toEqual({
      ok: false,
      reason: 'provider-error',
      error: { name: 'AccessDeniedError', message: 'user denied download' },
    });
  });

  it('does not trigger providers during bridge construction', () => {
    const shareMessage = availableAsync(async (_id: string) => undefined);
    const downloadFile = availableAsync(
      async (_url: string, _fileName: string) => undefined,
    );
    createTelegramTransferBridge({
      isShareMessageSupported: () => true,
      isDownloadFileSupported: () => true,
      shareMessage,
      downloadFile,
    });

    expect(shareMessage).not.toHaveBeenCalled();
    expect(downloadFile).not.toHaveBeenCalled();
  });
});
