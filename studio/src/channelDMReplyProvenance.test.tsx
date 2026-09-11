import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { ChannelDMProvenance } from './ChannelDMProvenance';
import type { ChannelDMView } from './types';


const ordinaryDM: ChannelDMView = {
  transport: 'telegram_channel_dms',
  sender: { id: 501, username: 'alice', display_name: '@alice' },
  topic_user: { id: 501, username: 'alice', display_name: '@alice' },
  topic_id: 123,
  received_at: '2026-08-18T02:00:00+00:00',
  edited_at: '2026-08-18T02:05:00+00:00',
  is_reply: true,
  reply_to: { chat_id: -1009001, message_id: 70 },
  media_group_id: 'album-1',
  direct_messages_chat_id: -1009001,
  message_id: 77,
};


describe('Channel DM reply preserves T5.2 provenance UX', () => {
  it('keeps ordinary DM provenance facts visible alongside the explicit composer', () => {
    const html = renderToStaticMarkup(createElement(ChannelDMProvenance, {
      candidateId: 55,
      channelDM: ordinaryDM,
      candidateStatus: 'new',
    }));

    expect(html).toContain('Ответить в Telegram');
    expect(html).toContain('От: @alice');
    expect(html).toContain('DM topic: @alice');
    expect(html).toContain('Текущее сообщение отредактировано');
    expect(html).toContain('Ответ на предыдущее сообщение');
    expect(html).toContain('media group');
    expect(html).toContain('DM chat -1009001');
    expect(html).toContain('message 77');
    expect(html).toContain('topic 123');
  });
});
