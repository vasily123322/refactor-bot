import type {
  ContentCandidateView,
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
  'pending', 'approved', 'declined', 'approval_failed', 'paid', 'refunded', 'unknown',
]);

function safeStatus(value: unknown): SuggestedPostNativeStatus {
  if (typeof value === 'string' && knownStatuses.has(value as SuggestedPostNativeStatus)) {
    return value as SuggestedPostNativeStatus;
  }
  return 'unknown';
}

function safeCommercialKind(value: unknown): SuggestedPostCommercialKind {
  if (value === 'paid' || value === 'free' || value === 'unknown') return value;
  return 'unknown';
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

function safeInteger(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : null;
}

export function suggestedPostMoneyLabel(
  money: SuggestedPostMoneyView | null | undefined,
): string | null {
  if (!money) return null;
  if (money.kind === 'stars') {
    const stars = safeInteger(money.stars);
    const nanostar = safeInteger(money.nanostar_amount);
    if (stars === null) return 'Telegram Stars · сумма неизвестна';
    return nanostar !== null && nanostar > 0
      ? `${stars} Stars + ${nanostar} nanostars`
      : `${stars} Stars`;
  }
  if (money.kind === 'ton_nanograms') {
    const nanograms = safeInteger(money.ton_nanograms);
    return nanograms === null ? 'TON · сумма неизвестна' : `${nanograms} TON nanograms`;
  }
  const currency = typeof money.currency === 'string' && money.currency.trim()
    ? money.currency.trim().toUpperCase()
    : 'неизвестная валюта';
  const rawAmount = safeInteger(money.raw_amount);
  return rawAmount === null
    ? `${currency} · значение не интерпретировано`
    : `${currency} · raw ${rawAmount}`;
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
  paidEventLabel: string | null;
  refundedEventLabel: string | null;
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
    paidEventLabel: suggestedPostDateLabel(suggestedPost.paid_event_at),
    refundedEventLabel: suggestedPostDateLabel(suggestedPost.refunded_event_at),
  };
}

export function candidateInboxPrimaryText(candidate: ContentCandidateView): string {
  return suggestedPostPresentation(candidate.suggested_post)
    ? candidate.excerpt
    : candidate.summary || candidate.excerpt;
}

export function suggestedPostEnrichmentSummary(candidate: ContentCandidateView): string | null {
  if (!suggestedPostPresentation(candidate.suggested_post)) return null;
  return candidate.summary || null;
}
