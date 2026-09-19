import type { ContentCandidateView } from './types';

export const INBOX_HISTORY_PAGE_SIZE = 100;

export type InboxHistoryCursor = {
  publishedAt: string | null;
  id: number;
};

export function splitInboxHistoryPage(
  rows: ContentCandidateView[],
  pageSize = INBOX_HISTORY_PAGE_SIZE,
): { items: ContentCandidateView[]; hasMore: boolean } {
  const boundedSize = Math.max(1, pageSize);
  return {
    items: rows.slice(0, boundedSize),
    hasMore: rows.length > boundedSize,
  };
}

export function inboxHistoryCursor(
  items: ContentCandidateView[],
): InboxHistoryCursor | null {
  const last = items.at(-1);
  if (!last) return null;
  return { publishedAt: last.published_at, id: last.id };
}

export function mergeInboxHistoryPage(
  current: ContentCandidateView[],
  incoming: ContentCandidateView[],
): ContentCandidateView[] {
  const seen = new Set(current.map((item) => item.id));
  return [
    ...current,
    ...incoming.filter((item) => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    }),
  ];
}
