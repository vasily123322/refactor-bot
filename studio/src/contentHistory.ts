import type { ContentSummary } from './types';

export const CONTENT_HISTORY_PAGE_SIZE = 100;

export type ContentHistoryCursor = {
  updatedAt: string;
  id: number;
};

export function splitContentHistoryPage(
  rows: ContentSummary[],
  pageSize = CONTENT_HISTORY_PAGE_SIZE,
): { items: ContentSummary[]; hasMore: boolean } {
  const boundedSize = Math.max(1, pageSize);
  return {
    items: rows.slice(0, boundedSize),
    hasMore: rows.length > boundedSize,
  };
}

export function contentHistoryCursor(
  items: ContentSummary[],
): ContentHistoryCursor | null {
  const last = items.at(-1);
  if (!last?.updated_at) return null;
  return { updatedAt: last.updated_at, id: last.id };
}

export function mergeContentHistoryPage(
  current: ContentSummary[],
  incoming: ContentSummary[],
): ContentSummary[] {
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
