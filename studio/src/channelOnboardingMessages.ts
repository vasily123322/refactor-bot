import type { ChannelOnboardingFlowResult } from './channelOnboardingFlow';

const FAILURE_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  banned: 'Этот канал заблокирован и не может быть подключён.',
  'not-channel': 'Выберите именно Telegram-канал.',
  'bot-not-present': 'Сначала добавьте бота в канал и повторите.',
  'bot-not-admin': 'Бот должен быть администратором канала.',
  'bot-missing-rights': 'Боту нужны права публикации, редактирования и удаления.',
  'requester-not-admin': 'Вы должны быть администратором выбранного канала.',
  'requester-missing-rights': 'Вам нужны права публикации, редактирования и удаления.',
  'owner-conflict': 'Этот канал уже связан с другим владельцем Studio.',
  'missing-channel-id': 'Сервер не подтвердил подключённый канал.',
});

export function channelOnboardingResultMessage(
  result: ChannelOnboardingFlowResult,
): Readonly<{ kind: 'success' | 'notice' | 'error'; text: string }> {
  if (result.ok) {
    return { kind: 'success', text: 'Канал подключён.' };
  }

  switch (result.status) {
    case 'cancelled':
      return { kind: 'notice', text: 'Выбор канала отменён.' };
    case 'unsupported':
      return {
        kind: 'error',
        text: 'Нативный выбор канала недоступен в этой версии Telegram.',
      };
    case 'expired':
      return { kind: 'error', text: 'Запрос выбора канала истёк. Повторите.' };
    case 'timeout':
      return {
        kind: 'error',
        text: 'Telegram отправил выбор, но Studio ещё не получила подтверждение. Обновите список позже.',
      };
    case 'server-failed':
      return {
        kind: 'error',
        text: FAILURE_MESSAGES[result.failureReason || ''] || 'Канал не прошёл серверную проверку прав.',
      };
    case 'native-error':
      return { kind: 'error', text: 'Telegram не смог открыть выбор канала.' };
    case 'server-error':
      return { kind: 'error', text: 'Не удалось подготовить или проверить канал. Повторите позже.' };
    case 'aborted':
      return { kind: 'notice', text: '' };
  }
}
