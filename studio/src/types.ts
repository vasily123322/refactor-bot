export type InlineButton = {
  text: string;
  url?: string;
  callback_data?: string;
};

export type TelegramEntity = {
  type:
    | 'bold'
    | 'italic'
    | 'underline'
    | 'strikethrough'
    | 'code'
    | 'pre'
    | 'text_link'
    | 'blockquote';
  offset: number;
  length: number;
  url?: string;
  language?: string;
};

export type RichMark = string | { type: string; url?: string; href?: string };
export type RichSegmentValue = { text: string; marks?: RichMark[] };
export type RichContentValue = string | RichSegmentValue[];
export type RichCaptionValue = RichContentValue | {
  text?: RichContentValue;
  credit?: RichContentValue;
};
export type RichListItem = string | {
  label?: string;
  content?: RichContentValue;
  text?: string;
};
export type RichMediaCollectionItem = {
  type?: 'media' | 'image';
  asset_id?: number;
  media_asset_id?: number;
  kind?: string;
  media_type?: string;
  caption?: RichCaptionValue;
  credit?: RichContentValue;
  [key: string]: unknown;
};

export type PostBlock = {
  id: string;
  type: string;
  text?: string;
  caption?: RichCaptionValue;
  entities?: TelegramEntity[];
  caption_entities?: TelegramEntity[];
  content?: RichContentValue;
  credit?: RichContentValue;
  summary?: RichContentValue;
  items?: Array<RichListItem | RichMediaCollectionItem>;
  size?: number;
  formula?: string;
  name?: string;
  is_open?: boolean;
  [key: string]: unknown;
};

export type PostDocument = {
  schema_version: number;
  mode: 'classic' | 'rich';
  blocks: PostBlock[];
  telegram: {
    buttons?: InlineButton[][];
    [key: string]: unknown;
  };
  metadata: Record<string, unknown>;
};

export type StudioUser = {
  tg_user_id: number;
  username: string | null;
  full_name: string | null;
};

export type Channel = {
  id: number;
  tg_chat_id: number;
  title: string | null;
  is_active: boolean;
};

export type ContentSummary = {
  id: number;
  channel_id: number;
  kind: string;
  status: string;
  title: string | null;
  current_revision: number;
  updated_at: string | null;
};

export type ContentDetail = ContentSummary & {
  document: PostDocument;
};

export type TelegramPreviewResult = {
  message_ids: number[];
};

export type Publication = {
  id: number;
  content_item_id: number;
  content_revision: number;
  channel_id: number;
  status: string;
  schedule_entry_id: number | null;
};

export type PlannerEntry = {
  schedule_id: number;
  channel_id: number;
  content_item_id: number;
  content_revision: number;
  content_title: string | null;
  content_kind: string;
  scheduled_at: string;
  timezone: string | null;
  schedule_status: string;
  repeat_rule: Record<string, unknown>;
  publication_id: number | null;
  publication_status: string | null;
  telegram_message_ids: number[] | null;
  result_link: string | null;
  last_error: string | null;
  attempt_number: number | null;
  attempt_status: string | null;
  attempt_started_at: string | null;
  attempt_finished_at: string | null;
};

export type SourceConnectorView = {
  id: number;
  channel_id: number;
  kind: 'telegram' | 'rss' | 'url' | string;
  value: string;
  enabled: boolean;
  mode: string;
  citation_enabled: boolean;
  reuse_policy: string;
  status: string;
  status_reason: string | null;
  auth_state: string;
  capabilities: Record<string, unknown>;
  health: Record<string, unknown>;
  last_success_at: string | null;
  last_error_at: string | null;
  last_document_at: string | null;
  legacy_ai_source_id: number | null;
  legacy_grab_source_id: number | null;
  cursor_message_id: number | null;
  backlog_hint: boolean;
  worker_failure_count: number;
  worker_retry_after: string | null;
  ingestion_busy: boolean;
  ingestion_holder: string | null;
  ingestion_lease_expires_at: string | null;
};

export type SourceIngestionResult = {
  connector_id: number;
  documents_seen: number;
  documents_created: number;
  candidates_created: number;
};

export type SuggestedPostNativeStatus =
  | 'pending'
  | 'approved'
  | 'declined'
  | 'approval_failed'
  | 'paid'
  | 'refunded'
  | 'unknown';

export type SuggestedPostCommercialKind = 'free' | 'paid' | 'unknown';
export type SuggestedPostMoneyKind = 'stars' | 'ton_nanograms' | 'unknown';
export type SuggestedPostRefundReasonCode = 'post_deleted' | 'payment_refunded' | 'unknown';

export type SuggestedPostPersonView = {
  id: number | null;
  username: string | null;
  display_name: string | null;
};

export type SuggestedPostMoneyView = {
  kind: SuggestedPostMoneyKind;
  currency: string | null;
  stars: number | null;
  nanostar_amount: number | null;
  ton_nanograms: number | null;
  raw_amount: number | null;
};

export type SuggestedPostView = {
  transport: 'telegram_suggested_posts';
  native_status: SuggestedPostNativeStatus;
  commercial_kind: SuggestedPostCommercialKind;
  sender: SuggestedPostPersonView | null;
  topic_user: SuggestedPostPersonView | null;
  topic_id: number | null;
  direct_messages_chat_id: number | null;
  message_id: number | null;
  proposed_send_date: string | null;
  price: SuggestedPostMoneyView | null;
  payment: SuggestedPostMoneyView | null;
  paid_event_at: string | null;
  paid_service_message_id: number | null;
  refunded_event_at: string | null;
  refunded_service_message_id: number | null;
  decline_comment: string | null;
  refund_reason: string | null;
  refund_reason_code: SuggestedPostRefundReasonCode | null;
};

export type ContentCandidateView = {
  id: number;
  source_document_id: number;
  connector_id: number;
  status: string;
  suggested_action: string | null;
  score: number | null;
  topic: string | null;
  summary: string | null;
  source_title: string | null;
  source_url: string | null;
  excerpt: string;
  published_at: string | null;
  fetched_at: string | null;
  created_at: string | null;
  reuse_policy: string;
  suggested_post?: SuggestedPostView | null;
};

export const emptyTextDocument = (): PostDocument => ({
  schema_version: 1,
  mode: 'classic',
  blocks: [{ id: 'b1', type: 'text', text: '', entities: [] }],
  telegram: {},
  metadata: {},
});

export const emptyRichDocument = (): PostDocument => ({
  schema_version: 1,
  mode: 'rich',
  blocks: [{ id: 'p1', type: 'paragraph', content: '' }],
  telegram: {},
  metadata: {},
});

export function richContentText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value
      .map((part) =>
        typeof part === 'object' && part !== null && 'text' in part
          ? String((part as { text?: unknown }).text ?? '')
          : '',
      )
      .join('');
  }
  return '';
}

export function richCaptionText(value: unknown): string {
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    return richContentText((value as { text?: unknown }).text);
  }
  return richContentText(value);
}

export function documentText(document: PostDocument): string {
  return document.blocks
    .map((block) => {
      if (typeof block.text === 'string') return block.text;
      if (block.caption !== undefined) return richCaptionText(block.caption);
      if (block.content !== undefined) return richContentText(block.content);
      if (block.summary !== undefined) return richContentText(block.summary);
      if (typeof block.formula === 'string') return block.formula;
      return '';
    })
    .filter(Boolean)
    .join('\n\n');
}
