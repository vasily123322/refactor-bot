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

export type CandidateRewriteResult = {
  candidate_id: number;
  candidate_status: string;
  run_id: number;
  run_status: string;
  provider: string;
  model: string | null;
  text: string;
  reused_existing: boolean;
  output: Record<string, unknown>;
};

export type LocalBatchEnrichmentResult = {
  selected: number;
  completed: number;
  reused: number;
  skipped_busy: number;
  failed: number;
};

export type AIUsageView = {
  configured: boolean;
  enabled: boolean;
  model: string | null;
  temperature: number | null;
  max_tokens: number | null;
  tokens_used_day: number;
  tokens_limit_day: number | null;
  tokens_used_month: number;
  tokens_limit_month: number | null;
};

export type AIActivityRunView = {
  kind: 'enrichment' | 'rewrite' | string;
  id: number;
  candidate_id: number;
  status: string;
  provider: string;
  model: string | null;
  input_chars: number;
  error_type: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
};

export type AIActivityView = {
  usage: AIUsageView;
  enrichment_counts: Record<string, number>;
  rewrite_counts: Record<string, number>;
  runs: AIActivityRunView[];
};

export type SourceWorkerTickView = {
  started_at: string;
  finished_at: string;
  window_selected: number;
  scheduled: number;
  processed: number;
  skipped_backoff: number;
  skipped_busy: number;
  lease_errors: number;
  failures: number;
  timeouts: number;
  ingestion_errors: number;
  unexpected_errors: number;
  new_documents: number;
  candidates_created: number;
  backlog_remaining: number;
  stopped_early: boolean;
  duration_ms: number;
};

export type SourceWorkerTotalsView = Omit<
  SourceWorkerTickView,
  'started_at' | 'finished_at' | 'stopped_early' | 'duration_ms'
>;

export type SourceWorkerHealthView = {
  running: boolean;
  started_at: string;
  ticks: number;
  history_size: number;
  last_tick: SourceWorkerTickView | null;
  history: SourceWorkerTickView[];
  totals: SourceWorkerTotalsView;
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
    channelId: number | null = null,
  ) =>
    request<TelegramPreviewResult>('/api/studio/preview/telegram', {
      method: 'POST',
      body: JSON.stringify({
        document,
        replace_message_ids: replaceMessageIds,
        channel_id: channelId,
      }),
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
  sourceWorkerHealth: () =>
    request<SourceWorkerHealthView>('/api/studio/source-worker/health'),
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
  rewriteCandidateAI: (channelId: number, candidateId: number) =>
    request<CandidateRewriteResult>(
      `/api/studio/channels/${channelId}/candidates/${candidateId}/rewrite/ai`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  enrichCandidatesLocalBatch: (channelId: number, limit = 25) =>
    request<LocalBatchEnrichmentResult>(
      `/api/studio/channels/${channelId}/candidates/enrich/local-batch`,
      { method: 'POST', body: JSON.stringify({ limit }) },
    ),
  aiActivity: (channelId: number, limit = 50) =>
    request<AIActivityView>(
      `/api/studio/channels/${channelId}/ai/activity?limit=${encodeURIComponent(String(limit))}`,
    ),
};
