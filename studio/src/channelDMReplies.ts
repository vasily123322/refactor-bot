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
  const initData = getRawInitData();
  if (!initData) {
    throw new ChannelDMReplyApiError(
      'Откройте Studio из Telegram, чтобы авторизоваться.',
      401,
    );
  }
  const response = await fetch(`/api/studio/candidates/${candidateId}/channel-dm-reply`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Init-Data': initData,
    },
    body: JSON.stringify({
      reply_text: text,
      idempotency_key: idempotencyKey,
    }),
  });
  if (!response.ok) {
    let detail = response.statusText || 'Channel DM reply failed';
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Keep the HTTP status text if the server did not return JSON.
    }
    throw new ChannelDMReplyApiError(detail, response.status);
  }
  return (await response.json()) as ChannelDMReplyResult;
}
