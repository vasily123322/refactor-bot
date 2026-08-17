export type TelegramRequestChatError = Readonly<{
  name: string;
  message: string;
}>;

export type TelegramRequestChatResult =
  | Readonly<{ ok: true; status: 'sent' }>
  | Readonly<{ ok: false; status: 'unsupported' }>
  | Readonly<{ ok: false; status: 'cancelled-or-not-sent' }>
  | Readonly<{ ok: false; status: 'error'; error: TelegramRequestChatError }>;

export type TelegramRequestChatProvider = Readonly<{
  requestChat(
    preparedRequestId: string,
    callback: (sent: boolean) => void,
  ): void;
}>;

export type TelegramRequestChatBridgeDependencies = Readonly<{
  isSupported(): boolean;
  getProvider(): TelegramRequestChatProvider | undefined;
}>;

function normalizedError(error: unknown): TelegramRequestChatError {
  if (error instanceof Error) {
    return {
      name: error.name || 'Error',
      message: error.message || 'Unknown error',
    };
  }
  return { name: 'Error', message: String(error) };
}

export function createTelegramRequestChatBridge(
  deps: TelegramRequestChatBridgeDependencies,
) {
  return async function requestChat(
    preparedRequestId: string,
  ): Promise<TelegramRequestChatResult> {
    const normalizedRequestId = preparedRequestId.trim();
    if (!normalizedRequestId || !deps.isSupported()) {
      return { ok: false, status: 'unsupported' };
    }

    const provider = deps.getProvider();
    if (!provider) return { ok: false, status: 'unsupported' };

    return new Promise<TelegramRequestChatResult>((resolve) => {
      let settled = false;
      const finish = (result: TelegramRequestChatResult) => {
        if (settled) return;
        settled = true;
        resolve(result);
      };

      try {
        provider.requestChat(normalizedRequestId, (sent) => {
          finish(
            sent
              ? { ok: true, status: 'sent' }
              : { ok: false, status: 'cancelled-or-not-sent' },
          );
        });
      } catch (error) {
        finish({ ok: false, status: 'error', error: normalizedError(error) });
      }
    });
  };
}
