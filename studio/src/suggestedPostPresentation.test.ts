import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  applyCurrentStructuredRewrite,
  loadCurrentStructuredRewritePreviews,
} from './candidateRewriteAuthority';
import {
  candidateInboxPrimaryText,
  suggestedPostEnrichmentSummary,
  suggestedPostMoneyLabel,
  suggestedPostPresentation,
} from './suggestedPostPresentation';
import type {
  ContentCandidateView,
  ContentDetail,
  PostDocument,
  SuggestedPostNativeStatus,
  SuggestedPostView,
} from './types';

const suggestedPost: SuggestedPostView = {
  transport: 'telegram_suggested_posts',
  native_status: 'pending',
  commercial_kind: 'free',
  sender: { id: 501, username: 'alice', display_name: '@alice' },
  topic_user: { id: 501, username: 'alice', display_name: '@alice' },
  topic_id: 123,
  direct_messages_chat_id: -1009001,
  message_id: 77,
  proposed_send_date: '2026-08-20T12:30:00Z',
  price: null,
  payment: null,
  decline_comment: null,
  refund_reason: null,
};

function candidate(status: string = 'new'): ContentCandidateView {
  return {
    id: 55,
    source_document_id: 44,
    connector_id: 9,
    status,
    suggested_action: 'rewrite',
    score: null,
    topic: null,
    summary: null,
    source_title: 'Suggested source',
    source_url: null,
    excerpt: 'Current reconciled proposal',
    published_at: null,
    fetched_at: null,
    created_at: null,
    reuse_policy: 'rewrite_with_attribution',
    suggested_post: suggestedPost,
  };
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe('Suggested Post origin and native lifecycle presentation', () => {
  it('identifies only the explicit persisted Suggested Posts transport', () => {
    expect(suggestedPostPresentation(suggestedPost)?.status).toBe('pending');
    expect(suggestedPostPresentation(undefined)).toBeNull();
    expect(suggestedPostPresentation(null)).toBeNull();
  });

  it.each([
    ['pending', 'Ожидает решения в Telegram'],
    ['approved', 'Одобрено в Telegram'],
    ['declined', 'Отклонено в Telegram'],
    ['approval_failed', 'Ошибка одобрения в Telegram'],
    ['paid', 'Оплачено в Telegram'],
    ['refunded', 'Возврат в Telegram'],
  ] as Array<[SuggestedPostNativeStatus, string]>)('%s stays a native Telegram state', (status, label) => {
    const presentation = suggestedPostPresentation({ ...suggestedPost, native_status: status });
    expect(presentation?.statusLabel).toBe(label);
  });

  it('renders paid and refunded business provenance without implying Content approval/publication', () => {
    const presentation = suggestedPostPresentation({
      ...suggestedPost,
      native_status: 'refunded',
      commercial_kind: 'paid',
      price: { currency: 'XTR', amount: 120, nanostar_amount: null },
      payment: { currency: 'XTR', amount: 120, nanostar_amount: 5 },
      refund_reason: 'post_deleted',
    });

    expect(presentation?.statusLabel).toBe('Возврат в Telegram');
    expect(presentation?.commercialLabel).toBe('Платное · 120 Stars');
    expect(presentation?.paymentLabel).toBe('120 Stars + 5 nanostars');
    expect(presentation?.statusLabel.toLowerCase()).not.toContain('content');
    expect(presentation?.statusLabel.toLowerCase()).not.toContain('опублик');
  });

  it('formats XTR and TON amounts without treating TON nanoton as whole TON', () => {
    expect(suggestedPostMoneyLabel({ currency: 'XTR', amount: 9, nanostar_amount: null })).toBe('9 Stars');
    expect(suggestedPostMoneyLabel({ currency: 'TON', amount: 500, nanostar_amount: null })).toBe('500 nanoton');
  });

  it('degrades missing optional sender, price and send date safely', () => {
    const presentation = suggestedPostPresentation({
      ...suggestedPost,
      sender: null,
      topic_user: null,
      proposed_send_date: null,
      commercial_kind: 'unknown',
      price: null,
      payment: null,
    });

    expect(presentation?.senderLabel).toBeNull();
    expect(presentation?.topicUserLabel).toBeNull();
    expect(presentation?.proposedSendDateLabel).toBeNull();
    expect(presentation?.commercialLabel).toBe('Условия оплаты неизвестны');
  });

  it('degrades unknown lifecycle values to a neutral state instead of crashing', () => {
    const malformed = {
      ...suggestedPost,
      native_status: 'future_state' as SuggestedPostNativeStatus,
    };
    expect(() => suggestedPostPresentation(malformed)).not.toThrow();
    expect(suggestedPostPresentation(malformed)?.status).toBe('unknown');
    expect(suggestedPostPresentation(malformed)?.statusLabel).toBe('Статус Telegram неизвестен');
  });

  it('keeps current reconciled source content primary after a native edit', () => {
    const edited = {
      ...candidate(),
      excerpt: 'Edited native Suggested Post text',
      summary: 'Older enrichment summary',
    };
    const ordinary = { ...edited, suggested_post: null };

    expect(candidateInboxPrimaryText(edited)).toBe('Edited native Suggested Post text');
    expect(suggestedPostEnrichmentSummary(edited)).toBe('Older enrichment summary');
    expect(candidateInboxPrimaryText(ordinary)).toBe('Older enrichment summary');
    expect(suggestedPostEnrichmentSummary(ordinary)).toBeNull();
  });

  it('is read-only presentation and performs no Telegram or Content request', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const before = JSON.stringify(suggestedPost);

    const presentation = suggestedPostPresentation(suggestedPost);

    expect(presentation).not.toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(JSON.stringify(suggestedPost)).toBe(before);
  });
});

describe('T3 proposal/apply lifecycle remains unchanged for Suggested Post candidates', () => {
  const document: PostDocument = {
    schema_version: 1,
    mode: 'rich',
    blocks: [{ id: 'p1', type: 'paragraph', content: 'Structured proposal' }],
    telegram: {},
    metadata: { ai_generation_kind: 'structured_post_document_v1' },
  };

  it('recovers a server-current structured preview for a Suggested Post candidate', async () => {
    const previews = await loadCurrentStructuredRewritePreviews(
      7,
      [candidate()],
      async (_channelId, candidateId) => ({
        candidate_id: candidateId,
        run_id: 42,
        provider: 'channel_ai_structured',
        model: 'model-v1',
        text: 'Structured proposal',
        document,
      }),
    );

    expect(previews[55].runId).toBe(42);
    expect(previews[55].kind).toBe('structured');
    expect(previews[55].provenanceStatus).toBe('current_at_last_server_check');
  });

  it('keeps Apply explicit and bound to the exact current run id', async () => {
    vi.stubEnv('VITE_DEV_INIT_DATA', 'signed-test-init-data');
    const applied: ContentDetail = {
      id: 900,
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

    const result = await applyCurrentStructuredRewrite(7, candidate().id, 42);

    expect(result.id).toBe(900);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/channels/7/candidates/55/rewrite/ai/structured/apply');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({ run_id: 42 });
  });
});
