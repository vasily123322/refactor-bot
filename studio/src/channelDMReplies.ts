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

export type ChannelDMReplyProposalResult = {
  candidate_id: number;
  reply_text: string;
  model: string;
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
