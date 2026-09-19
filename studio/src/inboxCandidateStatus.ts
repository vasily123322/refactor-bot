export type InboxCandidateStatus = 'new' | 'dismissed';

export const INBOX_STATUS_OPTIONS: Array<{
  status: InboxCandidateStatus;
  label: string;
}> = [
  { status: 'new', label: 'Новые' },
  { status: 'dismissed', label: 'Скрытые' },
];

export function inboxStatusScope(status: InboxCandidateStatus): string {
  return `inbox:${status}`;
}

export function inboxStatusPresentation(status: InboxCandidateStatus): {
  heading: string;
  countSuffix: string;
  emptyText: string;
  showActiveActions: boolean;
  showRestore: boolean;
} {
  if (status === 'dismissed') {
    return {
      heading: 'Скрытые кандидаты',
      countSuffix: 'в архиве',
      emptyText: 'Скрытых материалов нет.',
      showActiveActions: false,
      showRestore: true,
    };
  }
  return {
    heading: 'Новые кандидаты',
    countSuffix: 'в очереди редактора',
    emptyText: 'Inbox пуст. Источники и ingestion worker добавят новые материалы сюда.',
    showActiveActions: true,
    showRestore: false,
  };
}
