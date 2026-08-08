export type InlineButton = {
  text: string;
  url?: string;
  callback_data?: string;
};

export type PostBlock = {
  id: string;
  type: string;
  text?: string;
  caption?: string;
  content?: string | Array<{ text?: string }>;
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

export const emptyTextDocument = (): PostDocument => ({
  schema_version: 1,
  mode: 'classic',
  blocks: [{ id: 'b1', type: 'text', text: '' }],
  telegram: {},
  metadata: {},
});

export function documentText(document: PostDocument): string {
  return document.blocks
    .map((block) => {
      if (typeof block.text === 'string') return block.text;
      if (typeof block.caption === 'string') return block.caption;
      if (typeof block.content === 'string') return block.content;
      if (Array.isArray(block.content)) {
        return block.content.map((part) => part.text ?? '').join('');
      }
      return '';
    })
    .filter(Boolean)
    .join('\n\n');
}

export function withDocumentText(document: PostDocument, text: string): PostDocument {
  const first = document.blocks[0];
  if (!first || first.type !== 'text') {
    return {
      ...document,
      mode: 'classic',
      blocks: [{ id: 'b1', type: 'text', text }],
    };
  }
  return {
    ...document,
    blocks: [{ ...first, text }, ...document.blocks.slice(1)],
  };
}
