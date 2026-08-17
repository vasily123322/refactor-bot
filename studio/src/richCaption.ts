import type { RichContentValue } from './types';
import { richContentText } from './types';

export type RichCaptionSource = {
  caption?: unknown;
  credit?: unknown;
};

export type RichCaptionView = {
  text: RichContentValue;
  credit: RichContentValue;
  present: boolean;
};

function richContent(value: unknown): RichContentValue {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return structuredClone(value) as RichContentValue;
  return '';
}

function meaningful(value: RichContentValue): boolean {
  return richContentText(value).trim().length > 0;
}

export function richCaptionView(source: RichCaptionSource): RichCaptionView {
  const rawCaption = source.caption;
  if (
    typeof rawCaption === 'object'
    && rawCaption !== null
    && !Array.isArray(rawCaption)
  ) {
    const mapping = rawCaption as Record<string, unknown>;
    return {
      text: richContent(mapping.text),
      credit: richContent(mapping.credit),
      present: true,
    };
  }
  if (rawCaption !== null && rawCaption !== undefined) {
    return {
      text: richContent(rawCaption),
      credit: richContent(source.credit),
      present: true,
    };
  }
  return { text: '', credit: '', present: false };
}

export function richCaptionPatch(
  source: RichCaptionSource,
  patch: Partial<Pick<RichCaptionView, 'text' | 'credit'>>,
): { caption: RichContentValue | undefined; credit: RichContentValue | undefined } {
  const current = richCaptionView(source);
  const text = patch.text === undefined ? current.text : richContent(patch.text);
  const credit = patch.credit === undefined ? current.credit : richContent(patch.credit);
  const hasText = meaningful(text);
  const hasCredit = meaningful(credit);
  if (!hasText && !hasCredit) return { caption: undefined, credit: undefined };
  return {
    caption: text,
    credit: hasCredit ? credit : undefined,
  };
}

export function richCaptionPreview(
  source: RichCaptionSource,
): { text: string; credit: string } | null {
  const view = richCaptionView(source);
  if (!view.present) return null;
  const text = richContentText(view.text);
  const credit = richContentText(view.credit);
  if (!text.trim() && !credit.trim()) return null;
  return { text, credit };
}
