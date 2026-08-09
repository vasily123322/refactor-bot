import type { PlannerEntry } from './types';

type AttemptFields = Pick<PlannerEntry, 'attempt_number' | 'attempt_status'>;

export function plannerAttemptLabel(entry: AttemptFields): string | null {
  if (entry.attempt_number === null || !entry.attempt_status) return null;

  const labels: Record<string, string> = {
    sending: 'выполняется',
    published: 'завершена',
    failed: 'ошибка',
    skipped: 'пропущена',
    cancelled: 'отменена',
  };
  const status = labels[entry.attempt_status] || entry.attempt_status;
  return `Попытка #${entry.attempt_number} · ${status}`;
}
