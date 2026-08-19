import type { TelegramRequestChatResult } from './telegramRequestChat';

export type ChannelOnboardingRequestStatus =
  | 'reserved'
  | 'prepared'
  | 'processing'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'expired';

export type ChannelOnboardingPrepare = Readonly<{
  request_id: number;
  prepared_button_id: string;
  status: ChannelOnboardingRequestStatus;
  expires_at: string;
}>;

export type ChannelOnboardingStatus = Readonly<{
  request_id: number;
  status: ChannelOnboardingRequestStatus;
  expires_at: string;
  channel_id: number | null;
  failure_reason: string | null;
}>;

export type ChannelOnboardingFlowResult =
  | Readonly<{ ok: true; status: 'succeeded'; channelId: number }>
  | Readonly<{
      ok: false;
      status:
        | 'unsupported'
        | 'cancelled'
        | 'native-error'
        | 'server-failed'
        | 'expired'
        | 'timeout'
        | 'aborted'
        | 'server-error';
      failureReason?: string;
    }>;

export type ChannelOnboardingFlowDependencies = Readonly<{
  prepare(): Promise<ChannelOnboardingPrepare>;
  getStatus(requestId: number): Promise<ChannelOnboardingStatus>;
  cancel(requestId: number): Promise<ChannelOnboardingStatus>;
  requestNative(preparedButtonId: string): Promise<TelegramRequestChatResult>;
  sleep(ms: number, signal?: AbortSignal): Promise<void>;
}>;

const DEFAULT_POLL_INTERVAL_MS = 500;
const DEFAULT_MAX_POLLS = 40;
const MAX_TELEGRAM_REQUEST_ID = 2_147_483_647;

async function cancelBestEffort(
  deps: ChannelOnboardingFlowDependencies,
  requestId: number,
): Promise<ChannelOnboardingStatus | null> {
  try {
    return await deps.cancel(requestId);
  } catch {
    return null;
  }
}

function terminalResult(status: ChannelOnboardingStatus): ChannelOnboardingFlowResult | null {
  if (status.status === 'succeeded') {
    return Number.isInteger(status.channel_id) && Number(status.channel_id) > 0
      ? { ok: true, status: 'succeeded', channelId: Number(status.channel_id) }
      : { ok: false, status: 'server-failed', failureReason: 'missing-channel-id' };
  }
  if (status.status === 'failed') {
    const failureReason =
      typeof status.failure_reason === 'string' && status.failure_reason.trim()
        ? status.failure_reason
        : 'onboarding-failed';
    return {
      ok: false,
      status: 'server-failed',
      failureReason,
    };
  }
  if (status.status === 'cancelled') return { ok: false, status: 'cancelled' };
  if (status.status === 'expired') return { ok: false, status: 'expired' };
  return null;
}

function prepareIsUsable(prepared: ChannelOnboardingPrepare): boolean {
  return (
    Number.isInteger(prepared.request_id) &&
    prepared.request_id > 0 &&
    prepared.request_id <= MAX_TELEGRAM_REQUEST_ID &&
    prepared.status === 'prepared' &&
    typeof prepared.prepared_button_id === 'string' &&
    prepared.prepared_button_id.trim().length > 0
  );
}

export async function runNativeChannelOnboarding(
  deps: ChannelOnboardingFlowDependencies,
  options: Readonly<{
    signal?: AbortSignal;
    pollIntervalMs?: number;
    maxPolls?: number;
  }> = {},
): Promise<ChannelOnboardingFlowResult> {
  if (options.signal?.aborted) return { ok: false, status: 'aborted' };

  let prepared: ChannelOnboardingPrepare;
  try {
    prepared = await deps.prepare();
  } catch {
    return { ok: false, status: 'server-error' };
  }

  if (!prepareIsUsable(prepared)) {
    if (Number.isInteger(prepared.request_id) && prepared.request_id > 0) {
      await cancelBestEffort(deps, prepared.request_id);
    }
    return { ok: false, status: 'server-error' };
  }

  if (options.signal?.aborted) {
    await cancelBestEffort(deps, prepared.request_id);
    return { ok: false, status: 'aborted' };
  }

  let native: TelegramRequestChatResult;
  try {
    native = await deps.requestNative(prepared.prepared_button_id);
  } catch {
    await cancelBestEffort(deps, prepared.request_id);
    return { ok: false, status: 'native-error' };
  }

  if (!native.ok) {
    await cancelBestEffort(deps, prepared.request_id);
    if (native.status === 'unsupported') return { ok: false, status: 'unsupported' };
    if (native.status === 'cancelled-or-not-sent') {
      return { ok: false, status: 'cancelled' };
    }
    return { ok: false, status: 'native-error' };
  }

  const pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  const maxPolls = options.maxPolls ?? DEFAULT_MAX_POLLS;
  let receivedStatus = false;

  for (let poll = 0; poll < maxPolls; poll += 1) {
    if (options.signal?.aborted) return { ok: false, status: 'aborted' };
    try {
      const status = await deps.getStatus(prepared.request_id);
      if (status.request_id !== prepared.request_id) {
        return { ok: false, status: 'server-error' };
      }
      receivedStatus = true;
      const terminal = terminalResult(status);
      if (terminal) return terminal;
    } catch {
      // A transient status read must not manufacture authority or retry the native picker.
    }

    try {
      await deps.sleep(pollIntervalMs, options.signal);
    } catch {
      if (options.signal?.aborted) return { ok: false, status: 'aborted' };
      return { ok: false, status: 'server-error' };
    }
  }

  const cancelled = await cancelBestEffort(deps, prepared.request_id);
  if (
    cancelled?.request_id === prepared.request_id &&
    cancelled.status === 'succeeded'
  ) {
    const terminal = terminalResult(cancelled);
    if (terminal) return terminal;
  }

  try {
    const finalStatus = await deps.getStatus(prepared.request_id);
    if (finalStatus.request_id !== prepared.request_id) {
      return { ok: false, status: 'server-error' };
    }
    if (finalStatus.status === 'succeeded' || finalStatus.status === 'failed') {
      const terminal = terminalResult(finalStatus);
      if (terminal) return terminal;
    }
    if (finalStatus.status === 'expired') return { ok: false, status: 'expired' };
    // A cancelled status here is the best-effort timeout cancellation performed above,
    // not a native picker cancellation after Telegram already reported `sent`.
  } catch {
    // The request stays server-authoritative. A later refresh can still reveal success.
  }

  return { ok: false, status: receivedStatus ? 'timeout' : 'server-error' };
}

export function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(new Error('aborted'));
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      globalThis.clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      reject(new Error('aborted'));
    };
    const timer = globalThis.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}
