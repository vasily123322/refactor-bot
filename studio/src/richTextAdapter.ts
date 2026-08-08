import type { JSONContent } from '@tiptap/core';

export type RichMark = string | { type: string; url?: string; href?: string };
export type RichSegment = { text: string; marks?: RichMark[] };
export type RichContent = string | RichSegment[];

type EditorMark = { type: string; attrs?: Record<string, unknown> };

function editorMark(mark: RichMark): EditorMark | null {
  if (typeof mark === 'string') {
    if (mark === 'strike' || mark === 'strikethrough') return { type: 'strike' };
    if (['bold', 'italic', 'underline', 'code'].includes(mark)) return { type: mark };
    return null;
  }
  if (mark.type === 'link' || mark.type === 'url') {
    const href = mark.url || mark.href;
    return href ? { type: 'link', attrs: { href } } : null;
  }
  return editorMark(mark.type);
}

function richMark(mark: EditorMark): RichMark | null {
  if (['bold', 'italic', 'underline', 'code'].includes(mark.type)) return mark.type;
  if (mark.type === 'strike') return 'strikethrough';
  if (mark.type === 'link') {
    const href = typeof mark.attrs?.href === 'string' ? mark.attrs.href : '';
    return href ? { type: 'link', url: href } : null;
  }
  return null;
}

export function richContentToEditorJson(content: RichContent | undefined): JSONContent {
  const segments: RichSegment[] =
    typeof content === 'string'
      ? [{ text: content }]
      : Array.isArray(content)
        ? content
        : [];
  return {
    type: 'doc',
    content: [
      {
        type: 'paragraph',
        content: segments
          .filter((segment) => segment.text)
          .map((segment) => {
            const marks = (segment.marks ?? [])
              .map(editorMark)
              .filter((mark): mark is EditorMark => mark !== null);
            return {
              type: 'text',
              text: segment.text,
              ...(marks.length ? { marks } : {}),
            };
          }),
      },
    ],
  };
}

export function editorJsonToRichContent(json: JSONContent): RichSegment[] {
  const segments: RichSegment[] = [];
  const append = (text: string, marks: RichMark[] = []) => {
    if (!text) return;
    const previous = segments.at(-1);
    if (previous && JSON.stringify(previous.marks ?? []) === JSON.stringify(marks)) {
      previous.text += text;
    } else {
      segments.push({ text, ...(marks.length ? { marks } : {}) });
    }
  };

  const render = (node: JSONContent) => {
    if (node.type === 'text') {
      const marks = ((node.marks ?? []) as EditorMark[])
        .map(richMark)
        .filter((mark): mark is RichMark => mark !== null);
      append(node.text ?? '', marks);
      return;
    }
    if (node.type === 'hardBreak') {
      append('\n');
      return;
    }
    for (const child of node.content ?? []) render(child);
  };
  render(json);
  return segments;
}

export function richContentPlainText(content: RichContent | undefined): string {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.map((segment) => segment.text).join('');
}
