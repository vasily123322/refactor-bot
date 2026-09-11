import type { InlineButton, PostDocument } from './types';

export type InlineButtonMode = 'url' | 'callback';

export type TelegramInlineKeyboardState =
  | { kind: 'editable'; rows: InlineButton[][] }
  | { kind: 'invalid'; reason: string };

export type InlineButtonDraft = {
  text: string;
  mode: InlineButtonMode;
  value: string;
};

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).length;
}

function validUrl(value: string): boolean {
  const target = value.trim();
  return (
    (target.startsWith('http://') && target.length > 'http://'.length)
    || (target.startsWith('https://') && target.length > 'https://'.length)
    || (target.startsWith('tg://') && target.length > 'tg://'.length)
  );
}

export function validateInlineButtonDraft(draft: InlineButtonDraft): string | null {
  if (!draft.text.trim()) return 'Введите текст кнопки';
  if (draft.mode === 'url') {
    if (!validUrl(draft.value)) return 'URL должен начинаться с http://, https:// или tg://';
    return null;
  }
  // Persisted callback_data is trimmed by inlineButtonFromDraft(), so validate the
  // exact canonical bytes that will be written rather than the pre-normalized draft.
  const size = utf8Bytes(draft.value.trim());
  if (size < 1 || size > 64) return 'callback_data должен занимать от 1 до 64 bytes';
  return null;
}

export function inlineButtonDraft(button: InlineButton): InlineButtonDraft {
  if (typeof button.url === 'string' && button.url.trim()) {
    return { text: button.text, mode: 'url', value: button.url };
  }
  return {
    text: button.text,
    mode: 'callback',
    value: button.callback_data ?? '',
  };
}

export function inlineButtonFromDraft(draft: InlineButtonDraft): InlineButton | null {
  if (validateInlineButtonDraft(draft) !== null) return null;
  const text = draft.text.trim();
  const value = draft.value.trim();
  return draft.mode === 'url'
    ? { text, url: value }
    : { text, callback_data: value };
}

export function telegramInlineKeyboard(document: PostDocument): TelegramInlineKeyboardState {
  const raw = document.telegram.buttons;
  if (raw === null || raw === undefined) return { kind: 'editable', rows: [] };
  if (!Array.isArray(raw)) return { kind: 'invalid', reason: 'buttons должен быть массивом рядов' };

  const rows: InlineButton[][] = [];
  for (const rawRow of raw) {
    if (!Array.isArray(rawRow)) {
      return { kind: 'invalid', reason: 'каждый keyboard row должен быть массивом' };
    }
    const row: InlineButton[] = [];
    for (const rawButton of rawRow) {
      if (typeof rawButton !== 'object' || rawButton === null) {
        return { kind: 'invalid', reason: 'keyboard button должен быть объектом' };
      }
      const candidate = rawButton as Record<string, unknown>;
      const text = typeof candidate.text === 'string' ? candidate.text : '';
      // Mirror the production renderer action precedence (telegram_renderer.py):
      // `if url:` accepts any truthy raw url before callback_data, including
      // whitespace-only and non-string values. Select the same winner here and
      // let URL validation fail closed instead of falling back to callback_data.
      const rawUrl = candidate.url;
      const hasRawUrl = Boolean(rawUrl);
      const url = typeof rawUrl === 'string' ? rawUrl : String(rawUrl ?? '');
      const callbackData = typeof candidate.callback_data === 'string' ? candidate.callback_data : '';
      const draft: InlineButtonDraft = hasRawUrl
        ? { text, mode: 'url', value: url }
        : { text, mode: 'callback', value: callbackData };
      const button = inlineButtonFromDraft(draft);
      if (!button) {
        return { kind: 'invalid', reason: validateInlineButtonDraft(draft) ?? 'Некорректная кнопка' };
      }
      row.push(button);
    }
    if (row.length > 0) rows.push(row);
  }
  return { kind: 'editable', rows };
}

export function patchTelegramInlineKeyboard(
  document: PostDocument,
  rows: InlineButton[][],
): PostDocument | null {
  const normalized: InlineButton[][] = [];
  for (const row of rows) {
    const nextRow: InlineButton[] = [];
    for (const button of row) {
      const next = inlineButtonFromDraft(inlineButtonDraft(button));
      if (!next) return null;
      nextRow.push(next);
    }
    if (nextRow.length > 0) normalized.push(nextRow);
  }

  const telegram = { ...document.telegram };
  if (normalized.length > 0) telegram.buttons = normalized;
  else delete telegram.buttons;
  return { ...document, telegram };
}

export function moveKeyboardRow(
  rows: InlineButton[][],
  index: number,
  direction: -1 | 1,
): InlineButton[][] {
  const next = structuredClone(rows);
  const target = index + direction;
  if (index < 0 || index >= next.length || target < 0 || target >= next.length) return next;
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function moveKeyboardButton(
  rows: InlineButton[][],
  rowIndex: number,
  buttonIndex: number,
  direction: -1 | 1,
): InlineButton[][] {
  const next = structuredClone(rows);
  const row = next[rowIndex];
  if (!row) return next;
  const target = buttonIndex + direction;
  if (buttonIndex < 0 || buttonIndex >= row.length || target < 0 || target >= row.length) return next;
  [row[buttonIndex], row[target]] = [row[target], row[buttonIndex]];
  return next;
}