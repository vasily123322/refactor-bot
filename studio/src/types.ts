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

export type PostBlock = {
  id: string;
  type: string;
  text?: string;
  caption?: string;
  entities?: TelegramEntity[];
  caption_entities?: TelegramEntity[];
  content?: RichContentValue;
  credit?: RichContentValue;
  summary?: RichContentValue;
  items?: Array<string | { label?: string; content?: RichContentValue; text?: string }>;
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
  legacy_post_task_id: number | null;
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
  legacy_post_task_id: number | null;
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

export function documentText(document: PostDocument): string {
  return document.blocks
    .map((block) => {
      if (typeof block.text === 'string') return block.text;
      if (typeof block.caption === 'string') return block.caption;
      if (block.content !== undefined) return richContentText(block.content);
      if (block.summary !== undefined) return richContentText(block.summary);
      if (typeof block.formula === 'string') return block.formula;
      return '';
    })
    .filter(Boolean)
    .join('\n\n');
}
