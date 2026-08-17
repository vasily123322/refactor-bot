import { useEffect, useRef, useState } from 'react';

import { studioApi } from './api';
import {
  abortableSleep,
  runNativeChannelOnboarding,
} from './channelOnboardingFlow';
import { channelOnboardingResultMessage } from './channelOnboardingMessages';
import {
  getTelegramMiniAppCapabilities,
  requestTelegramChat,
} from './telegram';
import './channel-onboarding.css';

export function ChannelOnboardingControl({
  onConnected,
}: {
  onConnected: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{
    kind: 'success' | 'notice' | 'error';
    text: string;
  } | null>(null);
  const activeRef = useRef<AbortController | null>(null);
  const nativeSupported = getTelegramMiniAppCapabilities().requestChat;

  useEffect(
    () => () => {
      activeRef.current?.abort();
      activeRef.current = null;
    },
    [],
  );

  const start = async () => {
    if (busy || !nativeSupported) return;

    activeRef.current?.abort();
    const controller = new AbortController();
    activeRef.current = controller;
    setBusy(true);
    setMessage(null);

    try {
      const result = await runNativeChannelOnboarding(
        {
          prepare: studioApi.prepareChannelOnboarding,
          getStatus: studioApi.channelOnboardingStatus,
          cancel: studioApi.cancelChannelOnboarding,
          requestNative: requestTelegramChat,
          sleep: abortableSleep,
        },
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;

      if (result.ok) {
        try {
          await onConnected();
        } catch {
          if (!controller.signal.aborted) {
            setMessage({
              kind: 'error',
              text: 'Канал подключён, но список не обновился. Обновите Studio.',
            });
          }
          return;
        }
      }

      const nextMessage = channelOnboardingResultMessage(result);
      if (nextMessage.text) setMessage(nextMessage);
    } finally {
      if (activeRef.current === controller) {
        activeRef.current = null;
        if (!controller.signal.aborted) setBusy(false);
      }
    }
  };

  return (
    <div className="channel-onboarding-control">
      <button
        className="channel-button channel-onboarding-button"
        type="button"
        disabled={busy || !nativeSupported}
        onClick={() => void start()}
        title={
          nativeSupported
            ? 'Выбрать Telegram-канал через нативный picker'
            : 'Нативный выбор канала требует Telegram Mini Apps 9.6+'
        }
      >
        <span className="channel-avatar">+</span>
        <span className="channel-label">
          <strong>{busy ? 'Проверяю канал…' : 'Подключить канал'}</strong>
          <small>{nativeSupported ? 'Native Telegram picker' : 'Требуется Telegram 9.6+'}</small>
        </span>
      </button>
      {message && (
        <small
          className={`channel-onboarding-message channel-onboarding-${message.kind}`}
          role={message.kind === 'error' ? 'alert' : 'status'}
          aria-live="polite"
        >
          {message.text}
        </small>
      )}
    </div>
  );
}
