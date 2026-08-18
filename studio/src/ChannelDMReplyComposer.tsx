import { useEffect, useState } from 'react';

import {
  ChannelDMReplyApiError,
  channelDMReplyErrorLabel,
  channelDMReplyIntentOriginLabel,
  channelDMReplyIntentStateLabel,
  channelDMReplyLifecycleWarning,
  channelDMReplyStatusLabel,
  createAIChannelDMReplyIntent,
  createManualChannelDMReplyIntent,
  dismissChannelDMReplyIntent,
  editChannelDMReplyIntent,
  newChannelDMReplyKey,
  requestChannelDMReplyIntents,
  requestChannelDMReplyLifecycle,
  requestChannelDMReplyProposal,
  sendChannelDMReplyIntent,
  submitChannelDMReply,
  type ChannelDMReplyIntent,
  type ChannelDMReplyLifecycleCommand,
  type ChannelDMReplyResult,
} from './channelDMReplies';


type LocalDeliveryState = 'idle' | 'submitting' | 'request_unknown';

function lifecycleTime(value: string | null): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('ru-RU');
}

function intentFailureMessage(reason: unknown, fallback: string): string {
  return reason instanceof ChannelDMReplyApiError ? reason.message : fallback;
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
  const [intents, setIntents] = useState<ChannelDMReplyIntent[]>([]);
  const [intentsPending, setIntentsPending] = useState(true);
  const [intentsError, setIntentsError] = useState<string | null>(null);
  const [intentBusy, setIntentBusy] = useState<string | null>(null);

  const applyIntentRead = (next: ChannelDMReplyIntent[]) => {
    setIntents(next);
    const active = next.find((intent) => intent.state === 'pending_review' && !intent.handoff_in_progress);
    if (active) setText((current) => current || active.reply_text);
  };

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

  useEffect(() => {
    let active = true;
    setIntents([]);
    setIntentsPending(true);
    setIntentsError(null);
    void requestChannelDMReplyIntents(candidateId)
      .then((result) => {
        if (active) applyIntentRead(result.intents);
      })
      .catch((reason) => {
        if (!active) return;
        setIntentsError(
          `${intentFailureMessage(reason, 'Не удалось загрузить reply intents.')} `
          + 'Отправка заблокирована, пока review queue неизвестна.',
        );
      })
      .finally(() => {
        if (active) setIntentsPending(false);
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

  const retryIntentRead = () => {
    setIntentsPending(true);
    setIntentsError(null);
    void requestChannelDMReplyIntents(candidateId)
      .then((result) => applyIntentRead(result.intents))
      .catch((reason) => {
        setIntentsError(
          `${intentFailureMessage(reason, 'Не удалось загрузить reply intents.')} `
          + 'Отправка заблокирована, пока review queue неизвестна.',
        );
      })
      .finally(() => setIntentsPending(false));
  };

  const refreshHistory = () => {
    void requestChannelDMReplyLifecycle(candidateId)
      .then((result) => {
        setHistory(result.commands);
        setHistoryError(null);
      })
      .catch(() => {
        // Immediate command result remains visible. Readback failure never becomes
        // another Telegram mutation or a retry signal.
      });
  };

  const refreshIntents = () => {
    void requestChannelDMReplyIntents(candidateId)
      .then((result) => {
        applyIntentRead(result.intents);
        setIntentsError(null);
      })
      .catch(() => {
        // Keep the last durable response visible; never infer a send from read failure.
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

  const startNewDirectReply = (clearText = false) => {
    setCommandKey(null);
    setDelivery(null);
    setError(null);
    setProposalError(null);
    setLocalState('idle');
    if (clearText) setText('');
  };

  const runIntentAction = async (key: string, action: () => Promise<void>) => {
    if (intentBusy != null) return;
    setIntentBusy(key);
    setIntentsError(null);
    try {
      await action();
    } catch (reason) {
      setIntentsError(intentFailureMessage(reason, 'Не удалось изменить reply intent.'));
    } finally {
      setIntentBusy(null);
    }
  };

  const saveManualIntent = () => runIntentAction('manual', async () => {
    const result = await createManualChannelDMReplyIntent(candidateId, text);
    setText(result.intent.reply_text);
    refreshIntents();
  });

  const queueAIIntent = () => runIntentAction('ai', async () => {
    const result = await createAIChannelDMReplyIntent(candidateId);
    setText(result.intent.reply_text);
    refreshIntents();
  });

  const saveIntentEdit = (intent: ChannelDMReplyIntent) => runIntentAction(
    `edit:${intent.intent_id}`,
    async () => {
      await editChannelDMReplyIntent(intent.intent_id, text);
      refreshIntents();
    },
  );

  const dismissIntent = (intent: ChannelDMReplyIntent) => runIntentAction(
    `dismiss:${intent.intent_id}`,
    async () => {
      await dismissChannelDMReplyIntent(intent.intent_id);
      refreshIntents();
    },
  );

  const sendIntent = (intent: ChannelDMReplyIntent) => runIntentAction(
    `send:${intent.intent_id}`,
    async () => {
      const result = await sendChannelDMReplyIntent(intent.intent_id);
      setDelivery({
        command_id: result.command.command_id,
        candidate_id: result.command.candidate_id,
        state: result.command.state,
        provider_message_id: null,
        error_class: result.command.error_class,
        reused_existing: result.command.reused_existing,
      });
      setLocalState('idle');
      refreshHistory();
      refreshIntents();
    },
  );

  const intentLocked = localState === 'request_unknown' || delivery != null;
  const historyReady = !historyPending && historyError == null;
  const intentsReady = !intentsPending && intentsError == null;
  const currentIntent = intents.find((intent) => intent.state === 'pending_review') ?? null;
  const canSubmit = text.trim().length > 0
    && text.trim().length <= 4096
    && localState !== 'submitting'
    && !intentLocked
    && historyReady
    && intentsReady
    && currentIntent == null;
  const canPropose = localState === 'idle'
    && !intentLocked
    && commandKey == null
    && !proposalPending
    && currentIntent == null;
  const canCreateIntent = localState === 'idle'
    && !intentLocked
    && currentIntent == null
    && intentsReady
    && intentBusy == null;
  const canReconcileSameKey = commandKey != null
    && (localState === 'request_unknown' || delivery?.state === 'pending');
  const latestPersisted = history[0] ?? null;
  const persistedRisk = channelDMReplyLifecycleWarning(latestPersisted?.state ?? null);

  const prepareFailedRetry = () => {
    if (!latestPersisted || latestPersisted.state !== 'failed' || intentLocked || !historyReady) return;
    startNewDirectReply(false);
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
          className="button secondary compact"
          disabled={!canCreateIntent || !text.trim()}
          onClick={() => void saveManualIntent()}
        >
          {intentBusy === 'manual' ? 'Сохраняю…' : 'Сохранить как intent'}
        </button>
        <button
          className="button secondary compact"
          disabled={!canCreateIntent}
          onClick={() => void queueAIIntent()}
        >
          {intentBusy === 'ai' ? 'AI в очередь…' : 'AI в очередь'}
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
          <button className="button secondary compact" onClick={() => startNewDirectReply(false)}>
            Новая попытка
          </button>
        )}
        {delivery?.state === 'uncertain' && (
          <button className="button secondary compact" onClick={() => startNewDirectReply(false)}>
            Новая команда — может продублировать
          </button>
        )}
        {delivery?.state === 'sent' && (
          <button className="button secondary compact" onClick={() => startNewDirectReply(true)}>
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

      <div className="channel-dm-reply-history" aria-label="Durable reply intents">
        <strong>Reply intents · review queue</strong>
        {intentsPending && <small>Загрузка durable intents…</small>}
        {intentsError && (
          <div>
            <p className="channel-dm-reply-error" role="alert">{intentsError}</p>
            <button
              className="button secondary compact"
              disabled={intentsPending}
              onClick={retryIntentRead}
            >
              Повторить загрузку intents
            </button>
          </div>
        )}
        {intents.length === 0 && intentsReady && (
          <small>Durable proposal пока нет. AI/manual создание не отправляет Telegram message.</small>
        )}
        {intents.length > 0 && (
          <ul>
            {intents.map((intent) => {
              const deliveryFact = intent.consumed_command_id == null
                ? null
                : history.find((command) => command.command_id === intent.consumed_command_id) ?? null;
              const reviewable = intent.state === 'pending_review'
                && intent.is_current
                && !intent.handoff_in_progress
                && intentBusy == null
                && historyReady;
              return (
                <li key={intent.intent_id}>
                  <strong>Intent #{intent.intent_id}</strong>
                  {' · '}
                  <span>{channelDMReplyIntentOriginLabel(intent.origin)}</span>
                  {' · '}
                  <span>{channelDMReplyIntentStateLabel(intent.state)}</span>
                  {' · '}
                  <span>{lifecycleTime(intent.created_at)}</span>
                  <p>{intent.reply_text}</p>
                  {intent.handoff_in_progress && (
                    <p className="channel-dm-delivery-state channel-dm-delivery-unknown">
                      Handoff уже начат. Повторная проверка использует ту же server-side command identity;
                      новый Telegram send не создаётся скрыто.
                    </p>
                  )}
                  {intent.state === 'stale' && (
                    <>
                      <p className="channel-dm-delivery-state channel-dm-delivery-unknown">
                        Входящий DM изменился после proposal. Этот intent нельзя silently отправить.
                      </p>
                      <button
                        className="button secondary compact"
                        disabled={intentBusy != null || intentLocked}
                        onClick={() => setText(intent.reply_text)}
                      >
                        Скопировать stale text в редактор
                      </button>
                    </>
                  )}
                  {intent.state === 'pending_review' && !intent.handoff_in_progress && (
                    <div className="channel-dm-composer-actions">
                      <button
                        className="button secondary compact"
                        disabled={!reviewable || !text.trim()}
                        onClick={() => void saveIntentEdit(intent)}
                      >
                        {intentBusy === `edit:${intent.intent_id}` ? 'Сохраняю…' : 'Сохранить правку intent'}
                      </button>
                      <button
                        className="button secondary compact"
                        disabled={!reviewable}
                        onClick={() => void dismissIntent(intent)}
                      >
                        {intentBusy === `dismiss:${intent.intent_id}` ? 'Отклоняю…' : 'Отклонить intent'}
                      </button>
                      <button
                        className="button primary compact"
                        disabled={!reviewable || intentLocked || !intentsReady}
                        onClick={() => void sendIntent(intent)}
                      >
                        {intentBusy === `send:${intent.intent_id}` ? 'Передаю в T5.3…' : 'Явно отправить intent'}
                      </button>
                    </div>
                  )}
                  {intent.handoff_in_progress && (
                    <button
                      className="button secondary compact"
                      disabled={intentBusy != null || !historyReady || !intentsReady}
                      onClick={() => void sendIntent(intent)}
                    >
                      Восстановить связь с той же командой
                    </button>
                  )}
                  {intent.state === 'consumed' && intent.consumed_command_id != null && (
                    <p>
                      Command #{intent.consumed_command_id}
                      {deliveryFact
                        ? ` · ${channelDMReplyStatusLabel(deliveryFact.state)}`
                        : ' · delivery truth читается из T5.5 command lifecycle'}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        <small>
          ReplyIntent — proposal/review truth. Только явный Send создаёт T5.3 command;
          sent/failed/uncertain остаются исключительно delivery truth команды.
        </small>
      </div>

      {persistedRisk && (
        <p className="channel-dm-delivery-state channel-dm-delivery-unknown" role="status">
          {persistedRisk}
        </p>
      )}
      {latestPersisted?.state === 'failed' && !intentLocked && historyReady && intentsReady && (
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
      <small>AI proposal может стать durable intent · отправка остаётся отдельной явной T5.3 командой</small>
    </div>
  );
}
