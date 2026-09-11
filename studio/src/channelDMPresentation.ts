import type { ChannelDMPersonView, ChannelDMView, ContentCandidateView } from './types';

export type ChannelDMPresentation = {
  originLabel: 'Channel DM';
  senderLabel: string | null;
  topicUserLabel: string | null;
  receivedAtLabel: string | null;
  editedAtLabel: string | null;
  replyLabel: string | null;
  mediaGroupLabel: string | null;
};

function personLabel(person: ChannelDMPersonView | null | undefined): string | null {
  if (!person) return null;
  const display = person.display_name?.trim();
  if (display) return display;
  const username = person.username?.trim();
  if (username) return `@${username.replace(/^@+/, '')}`;
  return null;
}

function dateLabel(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat('ru-RU', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

export function channelDMPresentation(
  channelDM: ChannelDMView | null | undefined,
): ChannelDMPresentation | null {
  if (!channelDM || channelDM.transport !== 'telegram_channel_dms') return null;
  return {
    originLabel: 'Channel DM',
    senderLabel: personLabel(channelDM.sender),
    topicUserLabel: personLabel(channelDM.topic_user),
    receivedAtLabel: dateLabel(channelDM.received_at),
    editedAtLabel: dateLabel(channelDM.edited_at),
    replyLabel: channelDM.is_reply ? 'Ответ на предыдущее сообщение' : null,
    mediaGroupLabel: channelDM.media_group_id ? 'Часть media group' : null,
  };
}

export function channelDMEnrichmentSummary(candidate: ContentCandidateView): string | null {
  if (!channelDMPresentation(candidate.channel_dm)) return null;
  return candidate.summary || null;
}
