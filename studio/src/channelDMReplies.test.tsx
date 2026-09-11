import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChannelDMProvenance } from './ChannelDMProvenance';
import {
  channelDMReplyIntentStateLabel,
  channelDMReplyLifecycleWarning,
  channelDMReplyStatusLabel,
  createAIChannelDMReplyIntent,
  createManualChannelDMReplyIntent,
  dismissChannelDMReplyIntent,
  editChannelDMReplyIntent,
  requestChannelDMReplyIntents,
  requestChannelDMReplyLifecycle,
  requestChannelDMReplyProposal,
  sendChannelDMReplyIntent,
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

function lifecycle(candidateId: number, state: 'sent' | 'uncertain' = 'uncertain') {
  return {
    candidate_id: candidateId,
    commands: [{
      command_id: candidateId + 100,
      reply_text: `Persisted reply ${candidateId}`,
      state,
      requested_at: '2026-08-18T09:00:00+00:00',
      sent_at: state === 'sent' ? '2026-08-18T09:00:02+00:00' : null,
      finished_at: '2026-08-18T09:00:02+00:00',
      error_class: state === 'uncertain' ? 'provider_outcome_unknown' : null,
    }],
  };
}

function intent(candidateId: number, state: 'pending_review' | 'stale' | 'consumed' = 'pending_review') {
  return {
    intent_id: candidateId + 200,
    candidate_id: candidateId,
    reply_text: `Durable proposal ${candidateId}`,
    origin: 'ai' as const,
    state,
    is_current: state === 'pending_review',
    handoff_in_progress: false,
    consumed_command_id: state === 'consumed' ? candidateId + 100 : null,
    created_at: '2026-08-18T09:00:00+00:00',
    updated_at: '2026-08-18T09:00:01+00:00',
    consumed_at: state === 'consumed' ? '2026-08-18T09:00:02+00:00' : null,
    dismissed_at: null,
    stale_at: state === 'stale' ? '2026-08-18T09:00:02+00:00' : null,
  };
}

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function assertNoDeliveryAuthority(body: Record<string, unknown>) {
  for (const field of [
    'idempotency_key', 'chat_id', 'direct_messages_chat_id', 'direct_messages_topic_id',
    'message_thread_id', 'message_id', 'connector_id', 'channel_id', 'bot_id',
    'permission', 'can_manage_direct_messages', 'provider_message_id',
  ]) {
    expect(body).not.toHaveProperty(field);
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Channel DM reply request authority', () => {
  it('sends only reply text and idempotency key besides candidate identity', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({
      command_id: 90,
      candidate_id: 55,
      state: 'sent',
      provider_message_id: 901,
      error_class: null,
      reused_existing: false,
    }));
    vi.stubGlobal('fetch', fetchMock);

    await submitChannelDMReply(55, '  hello  ', 'dm-reply:fixed-request-key');

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/candidates/55/channel-dm-reply');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      reply_text: 'hello',
      idempotency_key: 'dm-reply:fixed-request-key',
    });
  });

  it('reads durable lifecycle through one coalesced narrow batch', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const ids = (JSON.parse(String(init?.body)) as { candidate_ids: number[] }).candidate_ids;
      return jsonResponse({ lifecycles: ids.map((id, index) => lifecycle(id, index ? 'sent' : 'uncertain')) });
    });
    vi.stubGlobal('fetch', fetchMock);

    const [first, second] = await Promise.all([
      requestChannelDMReplyLifecycle(55),
      requestChannelDMReplyLifecycle(56),
    ]);

    expect(first.candidate_id).toBe(55);
    expect(second.candidate_id).toBe(56);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/channel-dm-reply-lifecycle/batch');
    expect(JSON.parse(String(init?.body))).toEqual({ candidate_ids: [55, 56] });
  });

  it('keeps T5.4 AI proposal ephemeral and authority-free', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({
      candidate_id: 55,
      reply_text: 'Proposed draft only',
    }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await requestChannelDMReplyProposal(55);

    expect(result.reply_text).toBe('Proposed draft only');
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/candidates/55/channel-dm-reply-proposal');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({});
  });
});

describe('Durable Channel DM reply intent browser boundary', () => {
  it('coalesces reply-intent reload reads into one candidate-only batch', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const ids = (JSON.parse(String(init?.body)) as { candidate_ids: number[] }).candidate_ids;
      return jsonResponse({ candidates: ids.map((id) => ({ candidate_id: id, intents: [intent(id)] })) });
    });
    vi.stubGlobal('fetch', fetchMock);

    const [first, second] = await Promise.all([
      requestChannelDMReplyIntents(55),
      requestChannelDMReplyIntents(56),
    ]);

    expect(first.intents[0].reply_text).toBe('Durable proposal 55');
    expect(second.intents[0].reply_text).toBe('Durable proposal 56');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/channel-dm-reply-intents/batch');
    expect(JSON.parse(String(init?.body))).toEqual({ candidate_ids: [55, 56] });
  });

  it('manual save and edit carry only proposal text', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/manual')) {
        return jsonResponse({ intent: intent(55), reused_existing: false });
      }
      return jsonResponse({ ...intent(55), reply_text: 'edited intent' });
    });
    vi.stubGlobal('fetch', fetchMock);

    await createManualChannelDMReplyIntent(55, '  durable draft  ');
    await editChannelDMReplyIntent(255, '  edited intent  ');

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [createPath, createInit] = fetchMock.mock.calls[0];
    expect(createPath).toBe('/api/studio/candidates/55/channel-dm-reply-intents/manual');
    expect(createInit?.method).toBe('POST');
    const createBody = JSON.parse(String(createInit?.body));
    expect(createBody).toEqual({ reply_text: 'durable draft' });
    assertNoDeliveryAuthority(createBody);

    const [editPath, editInit] = fetchMock.mock.calls[1];
    expect(editPath).toBe('/api/studio/channel-dm-reply-intents/255');
    expect(editInit?.method).toBe('PATCH');
    const editBody = JSON.parse(String(editInit?.body));
    expect(editBody).toEqual({ reply_text: 'edited intent' });
    assertNoDeliveryAuthority(editBody);
  });

  it('AI queue, dismiss and explicit intent Send carry strict empty bodies', async () => {
    stubTelegramLaunch();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/ai')) return jsonResponse({ intent: intent(55), reused_existing: false });
      if (path.endsWith('/dismiss')) return jsonResponse({ ...intent(55), state: 'dismissed' });
      return jsonResponse({
        intent: { ...intent(55, 'consumed'), consumed_command_id: 501 },
        command: {
          command_id: 501,
          candidate_id: 55,
          state: 'uncertain',
          error_class: 'provider_outcome_unknown',
          reused_existing: false,
        },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    await createAIChannelDMReplyIntent(55);
    await dismissChannelDMReplyIntent(255);
    const sent = await sendChannelDMReplyIntent(255);

    expect(sent.intent.state).toBe('consumed');
    expect(sent.command.state).toBe('uncertain');
    expect(fetchMock).toHaveBeenCalledTimes(3);
    for (const [path, init] of fetchMock.mock.calls) {
      const body = JSON.parse(String(init?.body));
      expect(body).toEqual({});
      assertNoDeliveryAuthority(body);
      expect(path).not.toBe('/api/studio/candidates/55/channel-dm-reply');
    }
    expect(fetchMock.mock.calls[2][0]).toBe('/api/studio/channel-dm-reply-intents/255/send');
  });

  it('labels stale and consumed proposal state separately from delivery truth', () => {
    expect(channelDMReplyIntentStateLabel('stale')).toContain('Устарело');
    expect(channelDMReplyIntentStateLabel('consumed')).toContain('явную команду');
    expect(channelDMReplyIntentStateLabel('consumed')).not.toContain('Отправлено');
  });
});

describe('Channel DM reply visibility and status semantics', () => {
  it('renders proposal, durable review and explicit Send controls only for ordinary DM', () => {
    const ordinary = renderToStaticMarkup(createElement(ChannelDMProvenance, {
      candidateId: 55,
      channelDM: ordinaryDM,
      candidateStatus: 'new',
    }));
    expect(ordinary).toContain('Ответить в Telegram');
    expect(ordinary).toContain('Предложить с AI');
    expect(ordinary).toContain('Сохранить как intent');
    expect(ordinary).toContain('AI в очередь');
    expect(ordinary).toContain('Reply intents');
    expect(ordinary).not.toContain('Delivery status unknown');

    const nonDM = renderToStaticMarkup(createElement(ChannelDMProvenance, {
      candidateId: 55,
      channelDM: null,
      candidateStatus: 'new',
    }));
    expect(nonDM).toBe('');
  });

  it('keeps uncertain distinct and never relabels it as failed/sent', () => {
    expect(channelDMReplyStatusLabel('sent')).toBe('Отправлено');
    expect(channelDMReplyStatusLabel('failed')).toBe('Не отправлено');
    expect(channelDMReplyStatusLabel('uncertain')).toBe('Delivery status unknown');
    expect(channelDMReplyStatusLabel('pending')).not.toBe('Отправлено');
    expect(channelDMReplyLifecycleWarning('uncertain')).toContain('может продублировать');
    expect(channelDMReplyLifecycleWarning('failed')).toBeNull();
  });

  it('server rendering performs no HTTP/provider mutation', () => {
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
