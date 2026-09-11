import { getRawInitData } from './telegram';

export type ChannelDMReplyState = 'pending' | 'sent' | 'failed' | 'uncertain';

export type ChannelDMReplyResult = {
  command_id: number;
  candidate_id: number;
  state: ChannelDMReplyState;
  provider_message_id: number | null;
  error_class: string | null;
  reused_existing: boolean;
};

export type ChannelDMReplyLifecycleCommand = {
  command_id: number;
  reply_text: string;
  state: ChannelDMReplyState;
  requested_at: string;
  sent_at: string | null;
  finished_at: string | null;
  error_class: string | null;
};

export type ChannelDMReplyLifecycleResult = {
  candidate_id: number;
  commands: ChannelDMReplyLifecycleCommand[];
};

type ChannelDMReplyLifecycleBatchResult = {
  lifecycles: ChannelDMReplyLifecycleResult[];
};

export type ChannelDMReplyProposalResult = {
  candidate_id: number;
  reply_text: string;
};

export class ChannelDMReplyApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export function newChannelDMReplyKey(): string {
  return `dm-reply:${crypto.randomUUID()}`;
}

export function channelDMReplyStatusLabel(state: ChannelDMReplyState): string {
  switch (state) {
    case 'sent': return 'Отправлено';
    case 'failed': return 'Не отправлено';
    case 'uncertain': return 'Delivery status unknown';
    case 'pending': return 'Статус отправки уточняется';
  }
}

export function channelDMReplyErrorLabel(errorClass: string | null): string | null {
  switch (errorClass) {
    case null: return null;
    case 'routing_mismatch': return 'Маршрут Telegram изменился до отправки.';
    case 'routing_check_unavailable': return 'Не удалось подтвердить маршрут Telegram.';
    case 'rights_check_unavailable': return 'Не удалось подтвердить права Telegram.';
    case 'insufficient_rights': return 'Недостаточно прав для ответа в Channel DM.';
    case 'provider_forbidden': return 'Telegram запретил отправку.';
    case 'native_reply_target_missing': return 'Исходное сообщение больше недоступно для native reply.';
    case 'provider_rejected': return 'Telegram отклонил отправку.';
    case 'provider_retry_after': return 'Telegram отклонил текущую попытку из-за rate limit.';
    case 'provider_outcome_unknown': return 'Telegram мог принять сообщение, но результат не подтверждён.';
    case 'provider_confirmation_malformed': return 'Telegram ответил без надёжного подтверждения отправки.';
    default: return 'Детали ошибки скрыты; безопасный статус сохранён сервером.';
  }
}

export function channelDMReplyLifecycleWarning(state: ChannelDMReplyState | null): string | null {
  if (state === 'uncertain') {
    return 'Статус предыдущей доставки неизвестен. Новая отправка создаст отдельную команду и может продублировать сообщение в Telegram.';
  }
  if (state === 'pending') {
    return 'Предыдущая команда ещё не имеет подтверждённого результата. Новая отправка будет отдельной командой и может продублировать сообщение в Telegram.';
  }
  return null;
}

function studioHeaders(): Record<string, string> {
  const initData = getRawInitData();
  if (!initData) {
    throw new ChannelDMReplyApiError(
      'Откройте Studio из Telegram, чтобы авторизоваться.',
      401,
    );
  }
  return {
    'Content-Type': 'application/json',
    'X-Telegram-Init-Data': initData,
  };
}

async function responseError(response: Response, fallback: string): Promise<ChannelDMReplyApiError> {
  let detail = response.statusText || fallback;
  try {
    const payload = (await response.json()) as { detail?: string };
    detail = payload.detail || detail;
  } catch {
    // Keep the HTTP status text if the server did not return JSON.
  }
  return new ChannelDMReplyApiError(detail, response.status);
}

type LifecycleWaiter = {
  resolve: (result: ChannelDMReplyLifecycleResult) => void;
  reject: (reason: unknown) => void;
};

let lifecycleWaiters = new Map<number, LifecycleWaiter[]>();
let lifecycleBatchScheduled = false;

async function flushChannelDMReplyLifecycleBatch(): Promise<void> {
  const pending = lifecycleWaiters;
  lifecycleWaiters = new Map<number, LifecycleWaiter[]>();
  lifecycleBatchScheduled = false;
  const candidateIds = [...pending.keys()];
  if (candidateIds.length === 0) return;

  try {
    const response = await fetch('/api/studio/channel-dm-reply-lifecycle/batch', {
      method: 'POST',
      headers: studioHeaders(),
      body: JSON.stringify({ candidate_ids: candidateIds }),
    });
    if (!response.ok) {
      throw await responseError(response, 'Channel DM reply lifecycle unavailable');
    }
    const payload = (await response.json()) as ChannelDMReplyLifecycleBatchResult;
    const byCandidate = new Map(
      payload.lifecycles.map((lifecycle) => [lifecycle.candidate_id, lifecycle]),
    );
    for (const [candidateId, waiters] of pending) {
      const result = byCandidate.get(candidateId);
      if (!result) {
        const error = new ChannelDMReplyApiError('Channel DM reply lifecycle unavailable', 409);
        waiters.forEach((waiter) => waiter.reject(error));
        continue;
      }
      waiters.forEach((waiter) => waiter.resolve(result));
    }
  } catch (reason) {
    for (const waiters of pending.values()) {
      waiters.forEach((waiter) => waiter.reject(reason));
    }
  }
}

export function requestChannelDMReplyLifecycle(
  candidateId: number,
): Promise<ChannelDMReplyLifecycleResult> {
  return new Promise((resolve, reject) => {
    const normalized = Number(candidateId);
    const waiters = lifecycleWaiters.get(normalized) ?? [];
    waiters.push({ resolve, reject });
    lifecycleWaiters.set(normalized, waiters);
    if (!lifecycleBatchScheduled) {
      lifecycleBatchScheduled = true;
      queueMicrotask(() => {
        void flushChannelDMReplyLifecycleBatch();
      });
    }
  });
}

export async function requestChannelDMReplyProposal(
  candidateId: number,
): Promise<ChannelDMReplyProposalResult> {
  const response = await fetch(`/api/studio/candidates/${candidateId}/channel-dm-reply-proposal`, {
    method: 'POST',
    headers: studioHeaders(),
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw await responseError(response, 'Channel DM reply proposal failed');
  }
  return (await response.json()) as ChannelDMReplyProposalResult;
}

export async function submitChannelDMReply(
  candidateId: number,
  replyText: string,
  idempotencyKey: string,
): Promise<ChannelDMReplyResult> {
  const text = replyText.trim();
  if (!text || text.length > 4096) {
    throw new ChannelDMReplyApiError(
      !text ? 'Введите текст ответа.' : 'Ответ не может быть длиннее 4096 символов.',
      422,
    );
  }
  const response = await fetch(`/api/studio/candidates/${candidateId}/channel-dm-reply`, {
    method: 'POST',
    headers: studioHeaders(),
    body: JSON.stringify({
      reply_text: text,
      idempotency_key: idempotencyKey,
    }),
  });
  if (!response.ok) {
    throw await responseError(response, 'Channel DM reply failed');
  }
  return (await response.json()) as ChannelDMReplyResult;
}
