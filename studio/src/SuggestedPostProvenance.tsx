import { suggestedPostPresentation } from './suggestedPostPresentation';
import type { SuggestedPostView } from './types';
import './suggested-post.css';

export function SuggestedPostProvenance({
  suggestedPost,
  candidateStatus,
}: {
  suggestedPost: SuggestedPostView | null | undefined;
  candidateStatus: string;
}) {
  const presentation = suggestedPostPresentation(suggestedPost);
  if (!presentation || !suggestedPost) return null;

  const identityParts = [
    suggestedPost.direct_messages_chat_id == null
      ? null
      : `DM chat ${suggestedPost.direct_messages_chat_id}`,
    suggestedPost.message_id == null ? null : `message ${suggestedPost.message_id}`,
    suggestedPost.topic_id == null ? null : `topic ${suggestedPost.topic_id}`,
  ].filter(Boolean);

  return (
    <section
      className={`suggested-post-provenance suggested-post-${presentation.commercialKind}`}
      aria-label="Telegram Suggested Post provenance"
    >
      <div className="suggested-post-badges">
        <span className="suggested-post-origin">Telegram Suggested Post</span>
        <span className={`suggested-post-status suggested-post-status-${presentation.status}`}>
          {presentation.statusLabel}
        </span>
        <span className="suggested-post-commercial">{presentation.commercialLabel}</span>
      </div>

      <div className="suggested-post-facts">
        {presentation.senderLabel && <span>От: {presentation.senderLabel}</span>}
        {presentation.proposedSendDateLabel && (
          <span>Предложенная отправка: {presentation.proposedSendDateLabel}</span>
        )}
        {presentation.paymentLabel && <span>Получено: {presentation.paymentLabel}</span>}
        {suggestedPost.decline_comment && <span>Комментарий: {suggestedPost.decline_comment}</span>}
        {suggestedPost.refund_reason && <span>Причина возврата: {suggestedPost.refund_reason}</span>}
      </div>

      <p className="suggested-post-authority-note">
        Telegram lifecycle и оплата — отдельная provenance-информация. Они не создают,
        не применяют и не публикуют Content автоматически.
      </p>

      <details className="suggested-post-details">
        <summary>Telegram provenance</summary>
        <dl>
          <div><dt>Inbox candidate</dt><dd>{candidateStatus || 'unknown'}</dd></div>
          {presentation.topicUserLabel && (
            <div><dt>DM topic user</dt><dd>{presentation.topicUserLabel}</dd></div>
          )}
          {identityParts.length > 0 && (
            <div><dt>Native identity</dt><dd>{identityParts.join(' · ')}</dd></div>
          )}
          {presentation.priceLabel && (
            <div><dt>Proposed price</dt><dd>{presentation.priceLabel}</dd></div>
          )}
          {presentation.paymentLabel && (
            <div><dt>Payment</dt><dd>{presentation.paymentLabel}</dd></div>
          )}
        </dl>
      </details>
    </section>
  );
}
