import { ChannelDMReplyComposer } from './ChannelDMReplyComposer';
import { channelDMPresentation } from './channelDMPresentation';
import type { ChannelDMView } from './types';
import './channel-dm.css';

export function ChannelDMProvenance({
  channelDM,
  candidateStatus,
  candidateId,
}: {
  channelDM: ChannelDMView | null | undefined;
  candidateStatus: string;
  candidateId?: number;
}) {
  const presentation = channelDMPresentation(channelDM);
  if (!presentation || !channelDM) return null;

  const nativeIdentity = [
    channelDM.direct_messages_chat_id == null
      ? null
      : `DM chat ${channelDM.direct_messages_chat_id}`,
    channelDM.message_id == null ? null : `message ${channelDM.message_id}`,
    channelDM.topic_id == null ? null : `topic ${channelDM.topic_id}`,
  ].filter(Boolean);
  const replyIdentity = channelDM.reply_to?.message_id == null
    ? null
    : [
        channelDM.reply_to.chat_id == null ? null : `chat ${channelDM.reply_to.chat_id}`,
        `message ${channelDM.reply_to.message_id}`,
      ].filter(Boolean).join(' · ');

  return (
    <section className="channel-dm-provenance" aria-label="Telegram Channel DM provenance">
      <div className="channel-dm-badges">
        <span className="channel-dm-origin">{presentation.originLabel}</span>
        {presentation.editedAtLabel && <span className="channel-dm-edited">Текущее сообщение отредактировано</span>}
      </div>

      <div className="channel-dm-facts">
        {presentation.senderLabel && <span>От: {presentation.senderLabel}</span>}
        {presentation.topicUserLabel && <span>DM topic: {presentation.topicUserLabel}</span>}
        {presentation.receivedAtLabel && <span>Получено: {presentation.receivedAtLabel}</span>}
        {presentation.editedAtLabel && <span>Изменено: {presentation.editedAtLabel}</span>}
        {presentation.replyLabel && <span>{presentation.replyLabel}</span>}
        {presentation.mediaGroupLabel && <span>{presentation.mediaGroupLabel}</span>}
      </div>

      <p className="channel-dm-authority-note">
        Telegram provenance описывает входящее сообщение. Ответ отправляется только явной командой ниже;
        native routing повторно выводится сервером из candidate/source/connector authority.
      </p>

      {candidateId != null && <ChannelDMReplyComposer candidateId={candidateId} />}

      <details className="channel-dm-details">
        <summary>Telegram provenance</summary>
        <dl>
          <div><dt>Inbox candidate</dt><dd>{candidateStatus || 'unknown'}</dd></div>
          {nativeIdentity.length > 0 && (
            <div><dt>Native identity</dt><dd>{nativeIdentity.join(' · ')}</dd></div>
          )}
          {replyIdentity && (
            <div><dt>Reply target</dt><dd>{replyIdentity}</dd></div>
          )}
          {channelDM.media_group_id && (
            <div><dt>Media group</dt><dd>{channelDM.media_group_id}</dd></div>
          )}
        </dl>
      </details>
    </section>
  );
}
