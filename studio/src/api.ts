import { getRawInitData } from './telegram';
import type {
  Channel,
  ContentDetail,
  ContentSummary,
  PostDocument,
  Publication,
  StudioUser,
  TelegramPreviewResult,
} from './types';

export class StudioApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const initData = getRawInitData();
  if (!initData) {
    throw new StudioApiError('Откройте Studio из Telegram, чтобы авторизоваться.', 401);
  }

  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Init-Data': initData,
      ...init.headers,
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Keep status text when the body is not JSON.
    }
    throw new StudioApiError(detail || 'Studio API error', response.status);
  }
  return (await response.json()) as T;
}

export const studioApi = {
  me: () => request<StudioUser>('/api/studio/me'),
  channels: () => request<Channel[]>('/api/studio/channels'),
  content: (channelId: number) =>
    request<ContentSummary[]>(`/api/studio/channels/${channelId}/content?limit=100`),
  contentItem: (channelId: number, contentId: number) =>
    request<ContentDetail>(`/api/studio/channels/${channelId}/content/${contentId}`),
  createContent: (channelId: number, document: PostDocument, title?: string) =>
    request<ContentDetail>(`/api/studio/channels/${channelId}/content`, {
      method: 'POST',
      body: JSON.stringify({ document, title: title || null, kind: 'post' }),
    }),
  saveRevision: (channelId: number, contentId: number, document: PostDocument) =>
    request<ContentDetail>(
      `/api/studio/channels/${channelId}/content/${contentId}/revisions`,
      {
        method: 'POST',
        body: JSON.stringify({ document, source: 'studio', status: 'draft' }),
      },
    ),
  telegramPreview: (
    document: PostDocument,
    replaceMessageIds: number[] = [],
  ) =>
    request<TelegramPreviewResult>('/api/studio/preview/telegram', {
      method: 'POST',
      body: JSON.stringify({ document, replace_message_ids: replaceMessageIds }),
    }),
  publishNow: (channelId: number, contentId: number) =>
    request<Publication>(
      `/api/studio/channels/${channelId}/content/${contentId}/schedule`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
};
