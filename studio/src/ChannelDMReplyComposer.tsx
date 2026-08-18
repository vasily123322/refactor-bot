import { useEffect, useState } from 'react';

import {
  ChannelDMReplyApiError,
  channelDMReplyErrorLabel,
  channelDMReplyLifecycleWarning,
  channelDMReplyStatusLabel,
  newChannelDMReplyKey,
  requestChannelDMReplyLifecycle,
  requestChannelDMReplyProposal,
  submitChannelDMReply,
  type ChannelDMReplyLifecycleCommand,
  type ChannelDMReplyResult,
} from './channelDMReplies';


type LocalDeliveryState = 'idle' | 'submitting' | 'request_unknown';

function lifecycleTime(value: string | null): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('ru-RU');
}

export function ChannelDMReplyComposer({ candidateId }: { candidateId: number }) {
  const [text, setText] = useState('');
  const [commandKey, setCommandKey] = useState<string | null>(null);
  const [delivery, setDelivery] = useState<ChannelDMReplyResult | null>(null);
  const [localState, setLocalState] = useState<LocalDeliveryState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [proposalPending, setProposalPending] = useState(false);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [history, setHistory] = useState<ChannelDMReplyLifecycleCommand[]>([]);
  const [historyPending, setHistoryPending] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setHistory([]);
    setHistoryPending(true);
    setHistoryError(null);
    void requestChannelDMReplyLifecycle(candidateId)
      .then((result) => {
        if (active) setHistory(result.commands);
      })
      .catch((reason) => {
        if (!active) return;
        const detail = reason instanceof ChannelDMReplyApiError
          ? reason.message
          : 'Не удалось загрузить историю отправки.';
        setHistoryError(
          `${detail} Новая отправка заблокирована, пока durable status предыдущих команд неизвестен.`,
        );
      })
      .finally(() => {
        if (active) setHistoryPending(false);
      });
    return () => {
      active = false;
    };
  }, [candidateId]);

  const retryHistoryRead = () => {
    setHistoryPending(true);
    setHistoryError(null);
    void requestChannelDMReplyLifecycle(candidateId)
      .then((result) => {
        setHistory(result.commands);
      })
      .catch((reason) => {
        const detail = reason instanceof ChannelDMReplyApiError
          ? reason.message
          : 'Не удалось загрузить историю отправки.';
        setHistoryError(
          `${detail} Новая отправка заблокирована, пока durable status предыдущих команд неизвестен.`,
        );
      })
      .finally(() => {
        setHistoryPending(false);
      });
  };

  const refreshHistory = () => {
    void requestChannelDMReplyLifecycle(candidateId)
      .then((result) => {
        setHistory(result.commands);
        setHistoryError(null);
      })
      .catch(() => {
        // Immediate POST result remains visible. A failed readback must never cause
        // another Telegram mutation or turn a terminal result into a retry.
      });
  };

  const propose = async () => {
    if (proposalPending || localState === 'submitting' || commandKey != null || delivery != null) return;
    setProposalPending(true);
    setProposalError(null);
    try {
      const result = await requestChannelDMReplyProposal(candidateId);
      setText(result.reply_text);
    } catch (reason) {
      setProposalError(
        reason instanceof ChannelDMReplyApiError
          ? reason.message
          : 'Не удалось предложить ответ с AI.',
      );
    } finally {
      setProposalPending(false);
    }
  };

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
      refreshHistory();
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
    setProposalError(null);
    setLocalState('idle');
    if (clearText) setText('');
  };

  const intentLocked = localState === 'request_unknown' || delivery != null;
  const historyReady = !historyPending && historyError == null;
  const canSubmit = text.trim().length > 0
    && text.trim().length <= 4096
    && localState !== 'submitting'
    && !intentLocked
    && historyReady;
  const canPropose = localState === 'idle'
    && !intentLocked
    && commandKey == null
    && !proposalPending;
  const canReconcileSameKey = localState === 'request_unknown' || delivery?.state === 'pending';
  const latestPersisted = history[0] ?? null;
  const persistedRisk = channelDMReplyLifecycleWarning(latestPersisted?.state ?? null);

  const prepareFailedRetry = () => {
    if (!latestPersisted || latestPersisted.state !== 'failed' || intentLocked || !historyReady) return;
    startNewIntent(false);
    setText(latestPersisted.reply_text);
  };

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
          className="button secondary compact"
          disabled={!canPropose}
          onClick={() => void propose()}
        >
          {proposalPending ? 'AI предлагает…' : 'Предложить с AI'}
        </button>
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
        {delivery?.state === 'failed' && (
          <button className="button secondary compact" onClick={() => startNewIntent(false)}>
            Новая попытка
          </button>
        )}
        {delivery?.state === 'uncertain' && (
          <button className="button secondary compact" onClick={() => startNewIntent(false)}>
            Новая команда — может продублировать
          </button>
        )}
        {delivery?.state === 'sent' && (
          <button className="button secondary compact" onClick={() => startNewIntent(true)}>
            Новый ответ
          </button>
        )}
      </div>
      {proposalError && <p className="channel-dm-reply-error" role="alert">{proposalError}</p>}
      {localState === 'request_unknown' && (
        <p className="channel-dm-delivery-state channel-dm-delivery-unknown" role="status">
          Delivery status unknown. Автоповтора в Telegram нет.
        </p>
      )}
      {delivery && (
        <p className={`channel-dm-delivery-state channel-dm-delivery-${delivery.state}`} role="status">
          {channelDMReplyStatusLabel(delivery.state)}
          {delivery.state === 'uncertain' && ' · Автоповтора в Telegram нет.'}
          {channelDMReplyErrorLabel(delivery.error_class) && ` · ${channelDMReplyErrorLabel(delivery.error_class)}`}
        </p>
      )}
      {error && <p className="channel-dm-reply-error" role="alert">{error}</p>}

      {persistedRisk && (
        <p className="channel-dm-delivery-state channel-dm-delivery-unknown" role="status">
          {persistedRisk}
        </p>
      )}
      {latestPersisted?.state === 'failed' && !intentLocked && historyReady && (
        <button className="button secondary compact" onClick={prepareFailedRetry}>
          Повторить как новую команду
        </button>
      )}
      {historyPending && <small>Загрузка сохранённого статуса отправки…</small>}
      {historyError && (
        <div>
          <p className="channel-dm-reply-error" role="alert">{historyError}</p>
          <button
            className="button secondary compact"
            disabled={historyPending}
            onClick={retryHistoryRead}
          >
            Повторить загрузку статуса
          </button>
        </div>
      )}
      {history.length > 0 && (
        <div className="channel-dm-reply-history" aria-label="История явных ответов">
          <strong>Последние явные отправки</strong>
          <ul>
            {history.map((command) => {
              const completedAt = lifecycleTime(command.sent_at || command.finished_at);
              const safeError = channelDMReplyErrorLabel(command.error_class);
              return (
                <li key={command.command_id}>
                  <span>{channelDMReplyStatusLabel(command.state)}</span>
                  {' · '}
                  <span>{lifecycleTime(command.requested_at)}</span>
                  {completedAt && ` → ${completedAt}`}
                  {' · '}
                  <span>{command.reply_text}</span>
                  {safeError && ` · ${safeError}`}
                </li>
              );
            })}
          </ul>
          <small>
            История только показывает durable status. Любая новая отправка создаёт новую явную команду;
            старый результат не переотправляется автоматически.
          </small>
        </div>
      )}
      <small>AI только заполняет черновик · отправка остаётся отдельной явной командой</small>
    </div>
  );
}
