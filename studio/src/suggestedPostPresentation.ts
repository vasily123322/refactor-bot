import type {
  SuggestedPostCommercialKind,
  SuggestedPostMoneyView,
  SuggestedPostNativeStatus,
  SuggestedPostPersonView,
  SuggestedPostView,
} from './types';

const statusLabels: Record<SuggestedPostNativeStatus, string> = {
  pending: 'Ожидает решения в Telegram',
  approved: 'Одобрено в Telegram',
  declined: 'Отклонено в Telegram',
  approval_failed: 'Ошибка одобрения в Telegram',
  paid: 'Оплачено в Telegram',
  refunded: 'Возврат в Telegram',
  unknown: 'Статус Telegram неизвестен',
};

const knownStatuses = new Set<SuggestedPostNativeStatus>([
  'pending',
  'approved',
  'declined',
  'approval_failed',
  'paid',
  'refunded',
  'unknown',
]);

function safeStatus(value: unknown): SuggestedPostNativeStatus {
  return typeof value === 'string' && knownStatuses.has(value as SuggestedPostNativeStatus)
    ? value as SuggestedPostNativeStatus
    : 'unknown';
}

function safeCommercialKind(value: unknown): SuggestedPostCommercialKind {
  return value === 'paid' || value === 'free' || value === 'unknown' ? value : 'unknown';
}

function personLabel(person: SuggestedPostPersonView | null | undefined): string | null {
  if (!person) return null;
  if (typeof person.display_name === 'string' && person.display_name.trim()) {
    return person.display_name.trim();
  }
  if (typeof person.username === 'string' && person.username.trim()) {
    return `@${person.username.trim().replace(/^@/, '')}`;
  }
  return null;
}

export function suggestedPostMoneyLabel(
  money: SuggestedPostMoneyView | null | undefined,
): string | null {
  if (!money) return null;
  const currency = typeof money.currency === 'string' ? money.currency.trim().toUpperCase() : '';
  const amount = typeof money.amount === 'number' && Number.isFinite(money.amount)
    ? Math.trunc(money.amount)
    : null;
  const nanostar = typeof money.nanostar_amount === 'number' && Number.isFinite(money.nanostar_amount)
    ? Math.trunc(money.nanostar_amount)
    : null;

  if (currency === 'XTR') {
    if (amount === null) return 'Telegram Stars';
    return nanostar && nanostar > 0
      ? `${amount} Stars + ${nanostar} nanostars`
      : `${amount} Stars`;
  }
  if (currency === 'TON') {
    return amount === null ? 'TON' : `${amount} nanoton`;
  }
  if (amount !== null && currency) return `${amount} ${currency}`;
  if (amount !== null) return String(amount);
  return currency || null;
}

export function suggestedPostDateLabel(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat('ru', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

export type SuggestedPostPresentation = {
  status: SuggestedPostNativeStatus;
  statusLabel: string;
  commercialKind: SuggestedPostCommercialKind;
  commercialLabel: string;
  senderLabel: string | null;
  topicUserLabel: string | null;
  proposedSendDateLabel: string | null;
  priceLabel: string | null;
  paymentLabel: string | null;
};

export function suggestedPostPresentation(
  suggestedPost: SuggestedPostView | null | undefined,
): SuggestedPostPresentation | null {
  if (!suggestedPost || suggestedPost.transport !== 'telegram_suggested_posts') return null;

  const status = safeStatus(suggestedPost.native_status);
  const commercialKind = safeCommercialKind(suggestedPost.commercial_kind);
  const priceLabel = suggestedPostMoneyLabel(suggestedPost.price);
  const paymentLabel = suggestedPostMoneyLabel(suggestedPost.payment);
  const commercialLabel = commercialKind === 'paid'
    ? priceLabel ? `Платное · ${priceLabel}` : 'Платное предложение'
    : commercialKind === 'free'
      ? 'Бесплатное предложение'
      : 'Условия оплаты неизвестны';

  return {
    status,
    statusLabel: statusLabels[status],
    commercialKind,
    commercialLabel,
    senderLabel: personLabel(suggestedPost.sender),
    topicUserLabel: personLabel(suggestedPost.topic_user),
    proposedSendDateLabel: suggestedPostDateLabel(suggestedPost.proposed_send_date),
    priceLabel,
    paymentLabel,
  };
}
