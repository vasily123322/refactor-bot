import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChannelDMProvenance } from './ChannelDMProvenance';
import {
  channelDMEnrichmentSummary,
  channelDMPresentation,
} from './channelDMPresentation';
import {
  applyCurrentStructuredRewrite,
  loadCurrentStructuredRewritePreviews,
} from './candidateRewriteAuthority';
import {
  candidateInboxPrimaryText,
  suggestedPostPresentation,
} from './suggestedPostPresentation';
import type {
  ChannelDMView,
  ContentCandidateView,
  ContentDetail,
  PostDocument,
  SuggestedPostView,
} from './types';

const channelDM: ChannelDMView = {
  transport: 'telegram_channel_dms',
  sender: { id: 601, username: 'bob', display_name: '@bob' },
  topic_user: { id: 601, username: 'bob', display_name: '@bob' },
  topic_id: 321,
  received_at: '2026-08-18T02:00:00Z',
  edited_at: '2026-08-18T03:00:00Z',
  is_reply: true,
  reply_to: { chat_id: -1009101, message_id: 70 },
  media_group_id: 'album-a',
  direct_messages_chat_id: -1009101,
  message_id: 81,
};

const suggestedPost: SuggestedPostView = {
  transport: 'telegram_suggested_posts',
  native_status: 'pending',
  commercial_kind: 'free',
  sender: null,
  topic_user: null,
  topic_id: null,
  direct_messages_chat_id: -1009001,
  message_id: 77,
  proposed_send_date: null,
  price: null,
  payment: null,
  paid_event_at: null,
  paid_service_message_id: null,
  refunded_event_at: null,
  refunded_service_message_id: null,
  decline_comment: null,
  refund_reason: null,
  refund_reason_code: null,
};

function candidate(overrides: Partial<ContentCandidateView> = {}): ContentCandidateView {
  return {
    id: 55,
    source_document_id: 44,
    connector_id: 9,
    status: 'new',
    suggested_action: 'rewrite',
    score: null,
    topic: null,
    summary: null,
    source_title: null,
    source_url: null,
    excerpt: 'Current native DM body',
    published_at: '2026-08-18T02:00:00Z',
    fetched_at: null,
    created_at: null,
    reuse_policy: 'rewrite_with_attribution',
    suggested_post: null,
    channel_dm: channelDM,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Channel DM origin and read-only provenance presentation', () => {
  it('identifies only the explicit typed Channel DM transport', () => {
    expect(channelDMPresentation(channelDM)?.originLabel).toBe('Channel DM');
    expect(channelDMPresentation(null)).toBeNull();
    expect(channelDMPresentation(undefined)).toBeNull();
    expect(channelDMPresentation({
      ...channelDM,
      transport: 'telegram_suggested_posts' as never,
    })).toBeNull();
  });

  it('keeps Suggested Post and ordinary Channel DM as distinct origins', () => {
    expect(suggestedPostPresentation(suggestedPost)).not.toBeNull();
    expect(channelDMPresentation(suggestedPost as unknown as ChannelDMView)).toBeNull();
    expect(channelDMPresentation(channelDM)).not.toBeNull();
  });

  it('shows safe sender/topic/edit/reply/media facts without primary native identifiers', () => {
    const presentation = channelDMPresentation(channelDM);
    expect(presentation).not.toBeNull();
    expect(presentation?.senderLabel).toBe('@bob');
    expect(presentation?.topicUserLabel).toBe('@bob');
    expect(presentation?.editedAtLabel).not.toBeNull();
    expect(presentation?.replyLabel).toBe('Ответ на предыдущее сообщение');
    expect(presentation?.mediaGroupLabel).toBe('Часть media group');
    expect(JSON.stringify(presentation)).not.toContain('-1009101');
    expect(JSON.stringify(presentation)).not.toContain('"81"');
    expect(JSON.stringify(presentation)).not.toContain('album-a');
  });

  it('degrades missing optional sender/topic/reply/media metadata safely', () => {
    const presentation = channelDMPresentation({
      ...channelDM,
      sender: null,
      topic_user: null,
      received_at: null,
      edited_at: 'not-a-date',
      is_reply: false,
      reply_to: null,
      media_group_id: null,
    });
    expect(presentation?.senderLabel).toBeNull();
    expect(presentation?.topicUserLabel).toBeNull();
    expect(presentation?.receivedAtLabel).toBeNull();
    expect(presentation?.editedAtLabel).toBeNull();
    expect(presentation?.replyLabel).toBeNull();
    expect(presentation?.mediaGroupLabel).toBeNull();
  });

  it('keeps current reconciled DM content primary over stale enrichment after native edit', () => {
    const edited = candidate({
      excerpt: 'Current native DM body v2',
      summary: 'Stale enrichment summary from v1',
    });
    expect(candidateInboxPrimaryText(edited)).toBe('Current native DM body v2');
    expect(channelDMEnrichmentSummary(edited)).toBe('Stale enrichment summary from v1');

    const sameCardAfterEdit = { ...edited, excerpt: 'Current native DM body v3' };
    expect(sameCardAfterEdit.id).toBe(edited.id);
    expect(sameCardAfterEdit.source_document_id).toBe(edited.source_document_id);
    expect(candidateInboxPrimaryText(sameCardAfterEdit)).toBe('Current native DM body v3');
  });

  it('leaves ordinary non-DM candidate presentation unchanged', () => {
    const ordinary = candidate({
      channel_dm: null,
      excerpt: 'Original source body',
      summary: 'Ordinary enrichment summary',
    });
    expect(candidateInboxPrimaryText(ordinary)).toBe('Ordinary enrichment summary');
    expect(channelDMEnrichmentSummary(ordinary)).toBeNull();
  });

  it('renders native identifiers only inside diagnostics and no reply mutation controls', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const markup = renderToStaticMarkup(createElement(ChannelDMProvenance, {
      channelDM,
      candidateStatus: 'new',
    }));
    const detailsIndex = markup.indexOf('<details');
    expect(detailsIndex).toBeGreaterThan(0);
    const primaryMarkup = markup.slice(0, detailsIndex);
    expect(primaryMarkup).not.toContain('DM chat -1009101');
    expect(primaryMarkup).not.toContain('message 81');
    expect(primaryMarkup).not.toContain('album-a');
    expect(markup.indexOf('Native identity')).toBeGreaterThan(detailsIndex);
    expect(markup.indexOf('Reply target')).toBeGreaterThan(detailsIndex);
    expect(markup.indexOf('album-a')).toBeGreaterThan(detailsIndex);
    expect(markup).not.toContain('<button');
    expect(markup).not.toContain('<textarea');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not trigger AI, Content, or Telegram requests by presenting a Channel DM', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    expect(channelDMPresentation(channelDM)).not.toBeNull();
    expect(candidateInboxPrimaryText(candidate())).toBe('Current native DM body');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('existing T3 proposal/preview/Apply remains explicit for Channel DM candidates', () => {
  const document: PostDocument = {
    schema_version: 1,
    mode: 'rich',
    blocks: [{ id: 'p1', type: 'paragraph', content: 'Structured DM proposal' }],
    telegram: {},
    metadata: { ai_generation_kind: 'structured_post_document_v1' },
  };

  it('can recover the existing server-current structured preview', async () => {
    const previews = await loadCurrentStructuredRewritePreviews(
      7,
      [candidate()],
      async (_channelId, candidateId) => ({
        candidate_id: candidateId,
        run_id: 42,
        provider: 'channel_ai_structured',
        model: 'model-v1',
        text: 'Structured DM proposal',
        document,
      }),
    );
    expect(previews[55].runId).toBe(42);
    expect(previews[55].kind).toBe('structured');
    expect(previews[55].provenanceStatus).toBe('current_at_last_server_check');
  });

  it('does not Apply until the existing explicit Apply action is invoked with current run id', async () => {
    vi.stubGlobal('window', {
      location: { search: '?tgWebAppData=signed-test-init-data', hash: '' },
    });
    const applied: ContentDetail = {
      id: 901,
      channel_id: 7,
      kind: 'post',
      status: 'draft',
      title: null,
      current_revision: 1,
      updated_at: null,
      document,
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => (
      new Response(JSON.stringify(applied), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    ));
    vi.stubGlobal('fetch', fetchMock);

    expect(channelDMPresentation(channelDM)).not.toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();

    const result = await applyCurrentStructuredRewrite(7, candidate().id, 42);
    expect(result.id).toBe(901);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/channels/7/candidates/55/rewrite/ai/structured/apply');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({ run_id: 42 });
  });
});
