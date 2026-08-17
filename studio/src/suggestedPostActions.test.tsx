import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SuggestedPostProvenance } from './SuggestedPostProvenance';
import {
  SuggestedPostActionApiError,
  submitSuggestedPostAction,
} from './suggestedPostActions';
import type { ContentCandidateView, SuggestedPostView } from './types';


const pending: SuggestedPostView = {
  transport: 'telegram_suggested_posts',
  native_status: 'pending',
  commercial_kind: 'free',
  sender: { id: 501, username: 'alice', display_name: '@alice' },
  topic_user: { id: 501, username: 'alice', display_name: '@alice' },
  topic_id: 123,
  direct_messages_chat_id: -1009001,
  message_id: 77,
  proposed_send_date: null,
  price: null,
  payment: null,
  decline_comment: null,
  refund_reason: null,
};

function candidate(suggestedPost: SuggestedPostView | null = pending): ContentCandidateView {
  return {
    id: 55,
    source_document_id: 44,
    connector_id: 9,
    status: 'new',
    suggested_action: 'rewrite',
    score: null,
    topic: null,
    summary: null,
    source_title: 'Suggested source',
    source_url: null,
    excerpt: 'Current source content',
    published_at: null,
    fetched_at: null,
    created_at: null,
    reuse_policy: 'rewrite_with_attribution',
    suggested_post: suggestedPost,
  };
}

function stubTelegramLaunch() {
  vi.stubGlobal('window', {
    location: {
      search: '?tgWebAppData=signed-test-init-data',
      hash: '',
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Suggested Post action request authority', () => {
  it('sends approve with candidate identity only and no native routing authority', async () => {
    stubTelegramLaunch();
    const approved = candidate({ ...pending, native_status: 'approved' });
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => (
      new Response(JSON.stringify(approved), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    ));
    vi.stubGlobal('fetch', fetchMock);

    const result = await submitSuggestedPostAction(55, 'approve');

    expect(result.suggested_post?.native_status).toBe('approved');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/candidates/55/suggested-post-action');
    expect(init?.method).toBe('POST');
    const body = JSON.parse(String(init?.body));
    expect(body).toEqual({ action: 'approve' });
    expect(body).not.toHaveProperty('channel_id');
    expect(body).not.toHaveProperty('connector_id');
    expect(body).not.toHaveProperty('direct_messages_chat_id');
    expect(body).not.toHaveProperty('message_id');
    expect(body).not.toHaveProperty('native_status');
    expect(body).not.toHaveProperty('send_date');
  });

  it('sends only a validated optional decline comment besides the action', async () => {
    stubTelegramLaunch();
    const declined = candidate({ ...pending, native_status: 'declined' });
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => (
      new Response(JSON.stringify(declined), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    ));
    vi.stubGlobal('fetch', fetchMock);

    await submitSuggestedPostAction(55, 'decline', '  Not for us  ');

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({
      action: 'decline',
      comment: 'Not for us',
    });
  });

  it('enforces the Telegram decline comment boundary before request', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      submitSuggestedPostAction(55, 'decline', 'x'.repeat(129)),
    ).rejects.toBeInstanceOf(SuggestedPostActionApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('Suggested Post controls are plausibility-only UI', () => {
  it('renders explicit approve/decline controls only for a pending Suggested Post', () => {
    const html = renderToStaticMarkup(createElement(SuggestedPostProvenance, {
      candidateId: 55,
      suggestedPost: pending,
      candidateStatus: 'new',
    }));
    expect(html).toContain('Одобрить в Telegram');
    expect(html).toContain('Отклонить в Telegram');
    expect(html).toContain('maxLength="128"');
  });

  it('renders terminal Suggested Posts read-only', () => {
    const html = renderToStaticMarkup(createElement(SuggestedPostProvenance, {
      candidateId: 55,
      suggestedPost: { ...pending, native_status: 'approved' },
      candidateStatus: 'new',
    }));
    expect(html).toContain('Одобрено в Telegram');
    expect(html).not.toContain('Одобрить в Telegram');
    expect(html).not.toContain('Отклонить в Telegram');
  });

  it('ordinary candidates never receive Suggested Post mutation controls', () => {
    const html = renderToStaticMarkup(createElement(SuggestedPostProvenance, {
      candidateId: 55,
      suggestedPost: null,
      candidateStatus: 'new',
    }));
    expect(html).toBe('');
  });

  it('rendering a pending candidate performs no native mutation request', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    renderToStaticMarkup(createElement(SuggestedPostProvenance, {
      candidateId: 55,
      suggestedPost: pending,
      candidateStatus: 'new',
    }));

    expect(fetchMock).not.toHaveBeenCalled();
  });
});
