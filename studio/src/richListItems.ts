import type { RichContentValue, RichListItem } from './types';
import { richContentText } from './types';

export type RichListItemView = {
  label: string;
  content: RichContentValue;
};

function richContent(value: unknown): RichContentValue {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return structuredClone(value) as RichContentValue;
  if (value === null || value === undefined) return '';
  return String(value);
}

export function richListItems(value: unknown): RichListItem[] {
  if (!Array.isArray(value)) return [];
  return value.map((raw) => {
    if (typeof raw === 'string') return raw;
    if (typeof raw !== 'object' || raw === null) return String(raw ?? '');
    const candidate = raw as Record<string, unknown>;
    const label = candidate.label === null || candidate.label === undefined
      ? ''
      : String(candidate.label).trim();
    const content = richContent(candidate.content ?? candidate.text ?? '');
    if (!label && typeof content === 'string') return content;
    return {
      ...(label ? { label } : {}),
      content,
    };
  });
}

export function richListItemView(item: RichListItem): RichListItemView {
  if (typeof item === 'string') return { label: '', content: item };
  return {
    label: item.label?.trim() || '',
    content: richContent(item.content ?? item.text ?? ''),
  };
}

function serializeRichListItem(view: RichListItemView): RichListItem {
  const label = view.label.trim();
  const content = richContent(view.content);
  if (!label && typeof content === 'string') return content;
  return {
    ...(label ? { label } : {}),
    content,
  };
}

export function patchRichListItem(
  value: unknown,
  index: number,
  patch: Partial<RichListItemView>,
): RichListItem[] {
  const items = richListItems(value);
  if (index < 0 || index >= items.length) return items;
  return items.map((item, current) => {
    if (current !== index) return structuredClone(item);
    return serializeRichListItem({ ...richListItemView(item), ...patch });
  });
}

export function moveRichListItem(
  value: unknown,
  index: number,
  direction: -1 | 1,
): RichListItem[] {
  const items = richListItems(value);
  const target = index + direction;
  if (index < 0 || index >= items.length || target < 0 || target >= items.length) return items;
  const next = items.map((item) => structuredClone(item));
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function removeRichListItem(value: unknown, index: number): RichListItem[] {
  const items = richListItems(value);
  if (items.length <= 1 || index < 0 || index >= items.length) return items;
  return items.filter((_, current) => current !== index).map((item) => structuredClone(item));
}

export function appendRichListItem(value: unknown): RichListItem[] {
  return [...richListItems(value), 'Новый пункт'];
}

export function richListItemPreview(item: RichListItem): { label: string | null; text: string } {
  const view = richListItemView(item);
  return {
    label: view.label || null,
    text: richContentText(view.content),
  };
}
