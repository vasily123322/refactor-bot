import { useState } from 'react';

import {
  ChannelDMReplyApiError,
  channelDMReplyStatusLabel,
  newChannelDMReplyKey,
  submitChannelDMReply,
  type ChannelDMReplyResult,
} from './channelDMReplies';


type LocalDeliveryState = 'idle' | 'submitting' | 'request_unknown';

export function ChannelDMReplyComposer({ candidateId }: { candidateId: number }) {
  const [text, setText] = useState('');
  const [commandKey, setCommandKey] = useState<string | null>(null);
  const [delivery, setDelivery] = useState<ChannelDMReplyResult | null>(null);
  const [localState, setLocalState] = useState<LocalDeliveryState>('idle');
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const normalized = text.trim();
    if (!normalized || normalized.length > 4096 || localState === 'submitting') return;
    const key = commandKey || newChannelDMReplyKey();
    if (!commandKey) setCommandKey(key);
    setLocalState('submitting');
    setError(null);
    try {
      const result = await submitChannelDMReply(candidateId, normalized, key);
      setDelivery(result);
      setLocalState('idle');
    } catch (reason) {
      if (reason instanceof ChannelDMReplyApiError) {
        setError(reason.message);
        setLocalState('idle');
      } else {
        // The HTTP outcome is unknown. Keep the same key and lock the intent so
        // reconciliation cannot accidentally become a second Telegram command.
        setError(null);
        setLocalState('request_unknown');
      }
    }
  };

  const startNewIntent = (clearText = false) => {
    setCommandKey(null);
    setDelivery(null);
    setError(null);
    setLocalState('idle');
    if (clearText) setText('');
  };

  const intentLocked = localState === 'request_unknown' || delivery != null;
  const canSubmit = text.trim().length > 0
    && text.trim().length <= 4096
    && localState !== 'submitting'
    && !intentLocked;
  const canReconcileSameKey = localState === 'request_unknown' || delivery?.state === 'pending';

  return (
    <div className="channel-dm-composer" aria-label="Ответить в Telegram">
      <label htmlFor={`channel-dm-reply-${candidateId}`}>Ответить в Telegram</label>
      <textarea
        id={`channel-dm-reply-${candidateId}`}
        rows={3}
        maxLength={4096}
        value={text}
        disabled={localState === 'submitting' || intentLocked}
        placeholder="Текст ответа"
        onChange={(event) => setText(event.target.value)}
      />
      <div className="channel-dm-composer-actions">
        <button
          className="button primary compact"
          disabled={!canSubmit}
          onClick={() => void submit()}
        >
          {localState === 'submitting' ? 'Отправка…' : 'Отправить'}
        </button>
        {canReconcileSameKey && (
          <button
            className="button secondary compact"
            disabled={localState === 'submitting'}
            onClick={() => void submit()}
          >
            Проверить статус тем же запросом
          </button>
        )}
        {(delivery?.state === 'failed' || delivery?.state === 'uncertain') && (
          <button className="button secondary compact" onClick={() => startNewIntent(false)}>
            Новая попытка
          </button>
        )}
        {delivery?.state === 'sent' && (
          <button className="button secondary compact" onClick={() => startNewIntent(true)}>
            Новый ответ
          </button>
        )}
      </div>
      {localState === 'request_unknown' && (
        <p className="channel-dm-delivery-state channel-dm-delivery-unknown" role="status">
          Delivery status unknown. Автоповтора в Telegram нет.
        </p>
      )}
      {delivery && (
        <p className={`channel-dm-delivery-state channel-dm-delivery-${delivery.state}`} role="status">
          {channelDMReplyStatusLabel(delivery.state)}
          {delivery.state === 'uncertain' && ' · Автоповтора в Telegram нет.'}
        </p>
      )}
      {error && <p className="channel-dm-reply-error" role="alert">{error}</p>}
      <small>{text.trim().length}/4096 · новый explicit Send = новый command key</small>
    </div>
  );
}
