import { getRawInitData } from './telegram';
import type {
  Channel,
  ContentCandidateView,
  ContentDetail,
  ContentSummary,
  PlannerEntry,
  PostDocument,
  Publication,
  SourceConnectorView,
  SourceIngestionResult,
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

export type CreateSourceInput = {
  kind: 'telegram' | 'rss' | 'url';
  value: string;
  mode: 'summary' | 'rewrite';
  citation_enabled: boolean;
  reuse_policy:
    | 'reference_only'
    | 'summarize'
    | 'quote_with_attribution'
    | 'rewrite_with_attribution'
    | 'mirror_authorized';
};

export type CandidateEnrichmentResult = {
  candidate_id: number;
  candidate_status: string;
  summary: string | null;
  topic: string | null;
  score: number | null;
  run_id: number;
  run_status: string;
  provider: string;
  model: string | null;
  reused_existing: boolean;
  output: Record<string, unknown>;
};

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
  planner: (channelId: number, start: Date, end: Date) => {
    const query = new URLSearchParams({
      start: start.toISOString(),
      end: end.toISOString(),
      limit: '500',
    });
    return request<PlannerEntry[]>(`/api/studio/channels/${channelId}/planner?${query}`);
  },
  reschedule: (
    channelId: number,
    scheduleId: number,
    scheduledAt: Date,
    timezone?: string,
  ) =>
    request<PlannerEntry>(
      `/api/studio/channels/${channelId}/planner/${scheduleId}/reschedule`,
      {
        method: 'POST',
        body: JSON.stringify({
          scheduled_at: scheduledAt.toISOString(),
          timezone: timezone || null,
        }),
      },
    ),
  cancelSchedule: (channelId: number, scheduleId: number) =>
    request<PlannerEntry>(
      `/api/studio/channels/${channelId}/planner/${scheduleId}/cancel`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  sources: (channelId: number) =>
    request<SourceConnectorView[]>(`/api/studio/channels/${channelId}/sources`),
  createSource: (channelId: number, input: CreateSourceInput) =>
    request<SourceConnectorView>(`/api/studio/channels/${channelId}/sources`, {
      method: 'POST',
      body: JSON.stringify(input),
    }),
  sourceDoctor: (channelId: number, connectorId: number) =>
    request<SourceConnectorView>(
      `/api/studio/channels/${channelId}/sources/${connectorId}/doctor`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  ingestSource: (channelId: number, connectorId: number) =>
    request<SourceIngestionResult>(
      `/api/studio/channels/${channelId}/sources/${connectorId}/ingest`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  candidates: (channelId: number, status: 'new' | 'dismissed' = 'new') =>
    request<ContentCandidateView[]>(
      `/api/studio/channels/${channelId}/candidates?status_filter=${encodeURIComponent(status)}&limit=100`,
    ),
  dismissCandidate: (channelId: number, candidateId: number) =>
    request<ContentCandidateView>(
      `/api/studio/channels/${channelId}/candidates/${candidateId}/dismiss`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  candidateDraft: (channelId: number, candidateId: number) =>
    request<ContentDetail>(
      `/api/studio/channels/${channelId}/candidates/${candidateId}/draft`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  enrichCandidateLocal: (channelId: number, candidateId: number) =>
    request<CandidateEnrichmentResult>(
      `/api/studio/channels/${channelId}/candidates/${candidateId}/enrich/local`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  enrichCandidateAI: (channelId: number, candidateId: number) =>
    request<CandidateEnrichmentResult>(
      `/api/studio/channels/${channelId}/candidates/${candidateId}/enrich/ai`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
};
