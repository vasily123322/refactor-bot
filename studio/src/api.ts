import type {
  ChannelOnboardingPrepare,
  ChannelOnboardingStatus,
} from './channelOnboardingFlow';
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

async function parseResponse<T>(response: Response): Promise<T> {
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
  return parseResponse<T>(response);
}

async function multipartRequest<T>(path: string, form: FormData): Promise<T> {
  const initData = getRawInitData();
  if (!initData) {
    throw new StudioApiError('Откройте Studio из Telegram, чтобы авторизоваться.', 401);
  }
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'X-Telegram-Init-Data': initData },
    body: form,
  });
  return parseResponse<T>(response);
}

export type CreateSourceInput = {
  kind: 'telegram' | 'rss' | 'url' | 'telegram_channel_dms';
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

export type MediaAssetKind = 'photo' | 'video' | 'animation' | 'audio' | 'voice_note';

export type MediaAssetView = {
  id: number;
  channel_id: number;
  kind: MediaAssetKind | string;
  source: string;
  transport: 'telegram' | 'https';
  label: string | null;
  mime_type: string | null;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  size_bytes: number | null;
  created_at: string | null;
};

export type CreateMediaAssetInput = {
  kind: MediaAssetKind;
  telegram_file_id?: string | null;
  storage_url?: string | null;
  label?: string | null;
  mime_type?: string | null;
  width?: number | null;
  height?: number | null;
  duration_seconds?: number | null;
  size_bytes?: number | null;
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

export type CandidateStructuredRewriteResult = CandidateRewriteResult & {
  document: PostDocument;
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

export type AssistantScenario = 'attention_today' | 'drafts_tomorrow';

export type AssistantAttentionItem = {
  fact_id: string;
  category: string;
  severity: string;
  title: string;
  detail: string;
  refs: {
    schedule_entry_id?: number;
    publication_id?: number;
    content_item_id?: number;
    source_connector_id?: number;
  };
  suggested_action: string;
};

export type AssistantExecutionLimits = {
  max_steps: number;
  max_tool_calls: number;
  max_llm_calls: number;
  max_seconds: number;
};

export type AssistantAttentionRunResult = {
  scenario: 'attention_today';
  summary: string;
  attention_items: AssistantAttentionItem[];
  timezone: string;
  generated_by: 'llm_priority' | 'deterministic';
  tool_names: string[];
  execution_limits: AssistantExecutionLimits;
};

export type AssistantDraftReference = {
  content_item_id: number;
  content_revision: number;
  title: string;
  status: 'draft' | string;
};

export type AssistantDraftRunResult = {
  scenario: 'drafts_tomorrow';
  target_local_date: string;
  timezone: string;
  draft_count: number;
  drafts: AssistantDraftReference[];
  write_capability: 'draft_write';
  execution_limits: AssistantExecutionLimits;
};

export type AssistantRunResult = AssistantAttentionRunResult | AssistantDraftRunResult;

export type AssistantEventView = {
  id: number;
  sequence: number;
  event_type: string;
  tool_name: string | null;
  payload: Record<string, unknown>;
  created_at: string | null;
};

export type AssistantRunView = {
  id: number;
  channel_id: number;
  scenario: AssistantScenario;
  request_id: string | null;
  status: string;
  model: string | null;
  tokens_used: number;
  result: AssistantRunResult | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  events: AssistantEventView[];
};

export type AssistantApprovalState =
  | 'pending_review'
  | 'executing'
  | 'executed'
  | 'rejected'
  | 'stale'
  | 'failed';

export type AssistantApprovalView = {
  id: number;
  channel_id: number;
  source_admin_agent_run_id: number | null;
  action_type: 'schedule_draft_tomorrow' | string;
  state: AssistantApprovalState | string;
  content_item_id: number;
  content_revision: number;
  timezone: string;
  target_local_date: string;
  local_time: string;
  resolved_scheduled_at: string;
  action_fingerprint: string;
  execution_key: string | null;
  request_id: string;
  schedule_entry_id: number | null;
  publication_id: number | null;
  reviewer_tg_user_id: number | null;
  failure_reason: string | null;
  reviewed_at: string | null;
  executed_at: string | null;
  created_at: string | null;
};

export const studioApi = {
  me: () => request<StudioUser>('/api/studio/me'),
  channels: () => request<Channel[]>('/api/studio/channels'),
  prepareChannelOnboarding: () =>
    request<ChannelOnboardingPrepare>('/api/studio/channel-onboarding/prepare', {
      method: 'POST',
      body: JSON.stringify({}),
    }),
  channelOnboardingStatus: (requestId: number) =>
    request<ChannelOnboardingStatus>(`/api/studio/channel-onboarding/${requestId}`),
  cancelChannelOnboarding: (requestId: number) =>
    request<ChannelOnboardingStatus>(
      `/api/studio/channel-onboarding/${requestId}/cancel`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
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
  mediaAssets: (channelId: number, limit = 100) =>
    request<MediaAssetView[]>(
      `/api/studio/channels/${channelId}/media-assets?limit=${encodeURIComponent(String(limit))}`,
    ),
  createMediaAsset: (channelId: number, input: CreateMediaAssetInput) =>
    request<MediaAssetView>(`/api/studio/channels/${channelId}/media-assets`, {
      method: 'POST',
      body: JSON.stringify(input),
    }),
  uploadMediaAsset: (
    channelId: number,
    kind: MediaAssetKind,
    file: File,
    label: string | null = null,
  ) => {
    const form = new FormData();
    form.append('kind', kind);
    form.append('file', file, file.name);
    if (label) form.append('label', label);
    return multipartRequest<MediaAssetView>(
      `/api/studio/channels/${channelId}/media-assets/upload`,
      form,
    );
  },
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
  rewriteCandidateAIStructured: (channelId: number, candidateId: number) =>
    request<CandidateStructuredRewriteResult>(
      `/api/studio/channels/${channelId}/candidates/${candidateId}/rewrite/ai/structured`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  enrichCandidatesLocalBatch: (channelId: number, limit = 25) =>
    request<LocalBatchEnrichmentResult>(
      `/api/studio/channels/${channelId}/candidates/enrich/local-batch`,
      { method: 'POST', body: JSON.stringify({ limit }) },
    ),
  assistantRuns: (channelId: number, limit = 10) =>
    request<AssistantRunView[]>(
      `/api/studio/channels/${channelId}/assistant/runs?limit=${encodeURIComponent(String(limit))}`,
    ),
  createAssistantRun: (
    channelId: number,
    scenario: AssistantScenario,
    requestId: string | null = null,
  ) =>
    request<AssistantRunView>(`/api/studio/channels/${channelId}/assistant/runs`, {
      method: 'POST',
      body: JSON.stringify({
        scenario,
        ...(requestId ? { request_id: requestId } : {}),
      }),
    }),
  assistantRun: (channelId: number, runId: number) =>
    request<AssistantRunView>(
      `/api/studio/channels/${channelId}/assistant/runs/${runId}`,
    ),
  assistantApprovals: (channelId: number, limit = 50) =>
    request<AssistantApprovalView[]>(
      `/api/studio/channels/${channelId}/assistant/approvals?limit=${encodeURIComponent(String(limit))}`,
    ),
  createAssistantApproval: (
    channelId: number,
    contentItemId: number,
    localTime: string,
    requestId: string,
  ) =>
    request<AssistantApprovalView>(
      `/api/studio/channels/${channelId}/assistant/approvals`,
      {
        method: 'POST',
        body: JSON.stringify({
          content_item_id: contentItemId,
          local_time: localTime,
          request_id: requestId,
        }),
      },
    ),
  approveAssistantApproval: (channelId: number, approvalId: number) =>
    request<AssistantApprovalView>(
      `/api/studio/channels/${channelId}/assistant/approvals/${approvalId}/approve`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  rejectAssistantApproval: (channelId: number, approvalId: number) =>
    request<AssistantApprovalView>(
      `/api/studio/channels/${channelId}/assistant/approvals/${approvalId}/reject`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  aiActivity: (channelId: number, limit = 50) =>
    request<AIActivityView>(
      `/api/studio/channels/${channelId}/ai/activity?limit=${encodeURIComponent(String(limit))}`,
    ),
};
