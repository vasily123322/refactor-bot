import { useEffect, useState } from 'react';

import {
  submitSuggestedPostAction,
  type SuggestedPostAction,
} from './suggestedPostActions';
import { suggestedPostPresentation } from './suggestedPostPresentation';
import type { SuggestedPostView } from './types';
import './suggested-post.css';

function actionErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return 'Не удалось изменить Suggested Post.';
}

export function SuggestedPostProvenance({
  candidateId,
  suggestedPost,
  candidateStatus,
}: {
  candidateId: number;
  suggestedPost: SuggestedPostView | null | undefined;
  candidateStatus: string;
}) {
  const [current, setCurrent] = useState<SuggestedPostView | null | undefined>(suggestedPost);
  const [busyAction, setBusyAction] = useState<SuggestedPostAction | null>(null);
  const [declineComment, setDeclineComment] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    setCurrent(suggestedPost);
    setActionError(null);
  }, [suggestedPost]);

  const presentation = suggestedPostPresentation(current);
  if (!presentation || !current) return null;

  const identityParts = [
    current.direct_messages_chat_id == null
      ? null
      : `DM chat ${current.direct_messages_chat_id}`,
    current.message_id == null ? null : `message ${current.message_id}`,
    current.topic_id == null ? null : `topic ${current.topic_id}`,
  ].filter(Boolean);
  const canRequestAction = presentation.status === 'pending';

  const requestAction = async (action: SuggestedPostAction) => {
    if (busyAction !== null || !canRequestAction) return;
    setBusyAction(action);
    setActionError(null);
    try {
      const result = await submitSuggestedPostAction(
        candidateId,
        action,
        action === 'decline' ? declineComment : null,
      );
      if (!result.suggested_post) {
        throw new Error('Сервер не вернул актуальный Suggested Post status.');
      }
      // No optimistic terminal state: render only the server-returned read model.
      setCurrent(result.suggested_post);
      if (action === 'decline') setDeclineComment('');
    } catch (error) {
      setActionError(actionErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  };

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
        {current.decline_comment && <span>Комментарий: {current.decline_comment}</span>}
        {current.refund_reason && <span>Причина возврата: {current.refund_reason}</span>}
      </div>

      <p className="suggested-post-authority-note">
        Telegram lifecycle и оплата — отдельная provenance-информация. Они не создают,
        не применяют и не публикуют Content автоматически.
      </p>

      {canRequestAction && (
        <div className="suggested-post-actions">
          <div className="suggested-post-action-buttons">
            <button
              className="button primary compact"
              disabled={busyAction !== null}
              onClick={() => void requestAction('approve')}
            >
              {busyAction === 'approve' ? 'Одобряю…' : 'Одобрить в Telegram'}
            </button>
            <button
              className="button secondary compact"
              disabled={busyAction !== null}
              onClick={() => void requestAction('decline')}
            >
              {busyAction === 'decline' ? 'Отклоняю…' : 'Отклонить в Telegram'}
            </button>
          </div>
          <label>
            <span>Комментарий при отклонении · необязательно</span>
            <input
              type="text"
              maxLength={128}
              value={declineComment}
              disabled={busyAction !== null}
              onChange={(event) => setDeclineComment(event.target.value)}
              placeholder="До 128 символов"
            />
          </label>
          <small>
            Studio не задаёт новый send_date: native timing остаётся под authority Telegram.
          </small>
          {actionError && <div className="suggested-post-action-error" role="alert">{actionError}</div>}
        </div>
      )}

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
