import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChannelDMProvenance } from './ChannelDMProvenance';
import {
  channelDMReplyStatusLabel,
  requestChannelDMReplyProposal,
  submitChannelDMReply,
} from './channelDMReplies';
import type { ChannelDMView } from './types';


const ordinaryDM: ChannelDMView = {
  transport: 'telegram_channel_dms',
  sender: { id: 501, username: 'alice', display_name: '@alice' },
  topic_user: { id: 501, username: 'alice', display_name: '@alice' },
  topic_id: 123,
  received_at: '2026-08-18T02:00:00+00:00',
  edited_at: null,
  is_reply: false,
  reply_to: null,
  media_group_id: null,
  direct_messages_chat_id: -1009001,
  message_id: 77,
};

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

describe('Channel DM reply request authority', () => {
  it('sends only reply text and idempotency key besides candidate identity', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => (
      new Response(JSON.stringify({
        command_id: 90,
        candidate_id: 55,
        state: 'sent',
        provider_message_id: 901,
        error_class: null,
        reused_existing: false,
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    ));
    vi.stubGlobal('fetch', fetchMock);

    await submitChannelDMReply(55, '  hello  ', 'dm-reply:fixed-request-key');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/candidates/55/channel-dm-reply');
    expect(init?.method).toBe('POST');
    const body = JSON.parse(String(init?.body));
    expect(body).toEqual({
      reply_text: 'hello',
      idempotency_key: 'dm-reply:fixed-request-key',
    });
    for (const field of [
      'chat_id', 'direct_messages_chat_id', 'direct_messages_topic_id', 'message_thread_id',
      'message_id', 'connector_id', 'channel_id', 'bot_id', 'parse_mode',
    ]) {
      expect(body).not.toHaveProperty(field);
    }
  });

  it('AI proposal request carries no send authority and does not call the send endpoint', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => (
      new Response(JSON.stringify({
        candidate_id: 55,
        reply_text: 'Proposed draft only',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    ));
    vi.stubGlobal('fetch', fetchMock);

    const result = await requestChannelDMReplyProposal(55);

    expect(result).toEqual({ candidate_id: 55, reply_text: 'Proposed draft only' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/candidates/55/channel-dm-reply-proposal');
    expect(path).not.toBe('/api/studio/candidates/55/channel-dm-reply');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({});
  });

  it('rejects empty and oversized text before fetch', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await expect(submitChannelDMReply(55, '   ', 'dm-reply:key-empty')).rejects.toThrow();
    await expect(submitChannelDMReply(55, 'x'.repeat(4097), 'dm-reply:key-long')).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('Channel DM reply visibility and status semantics', () => {
  it('renders explicit AI draft and Send controls only when an ordinary DM read model exists', () => {
    const ordinary = renderToStaticMarkup(createElement(ChannelDMProvenance, {
      candidateId: 55,
      channelDM: ordinaryDM,
      candidateStatus: 'new',
    }));
    expect(ordinary).toContain('Ответить в Telegram');
    expect(ordinary).toContain('Предложить с AI');
    expect(ordinary).toContain('Отправить');
    expect(ordinary).not.toContain('Delivery status unknown');

    const nonDM = renderToStaticMarkup(createElement(ChannelDMProvenance, {
      candidateId: 55,
      channelDM: null,
      candidateStatus: 'new',
    }));
    expect(nonDM).toBe('');
  });

  it('maps server uncertainty separately from sent and never labels pending as sent', () => {
    expect(channelDMReplyStatusLabel('sent')).toBe('Отправлено');
    expect(channelDMReplyStatusLabel('failed')).toBe('Не отправлено');
    expect(channelDMReplyStatusLabel('uncertain')).toBe('Delivery status unknown');
    expect(channelDMReplyStatusLabel('pending')).not.toBe('Отправлено');
  });

  it('rendering the composer performs no provider HTTP mutation', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    renderToStaticMarkup(createElement(ChannelDMProvenance, {
      candidateId: 55,
      channelDM: ordinaryDM,
      candidateStatus: 'new',
    }));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
