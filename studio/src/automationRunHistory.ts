import type { AssistantAutomationRunSummaryView } from './api';

export const AUTOMATION_HISTORY_PAGE_SIZE = 20;

export type AutomationHistoryCursor = {
  scheduledFor: string;
  id: number;
};

export function splitAutomationHistoryPage(
  rows: AssistantAutomationRunSummaryView[],
  pageSize = AUTOMATION_HISTORY_PAGE_SIZE,
): { items: AssistantAutomationRunSummaryView[]; hasMore: boolean } {
  const bounded = Math.max(1, pageSize);
  return {
    items: rows.slice(0, bounded),
    hasMore: rows.length > bounded,
  };
}

export function automationHistoryCursor(
  rows: AssistantAutomationRunSummaryView[],
): AutomationHistoryCursor | null {
  const last = rows.at(-1);
  if (!last?.scheduled_for) return null;
  return { scheduledFor: last.scheduled_for, id: last.id };
}

export function mergeAutomationHistoryPage(
  current: AssistantAutomationRunSummaryView[],
  incoming: AssistantAutomationRunSummaryView[],
): AssistantAutomationRunSummaryView[] {
  const seen = new Set(current.map((row) => row.id));
  return [
    ...current,
    ...incoming.filter((row) => {
      if (seen.has(row.id)) return false;
      seen.add(row.id);
      return true;
    }),
  ];
}
