import type { JSONContent } from '@tiptap/core';

import type { PostBlock, PostDocument, TelegramEntity } from './types';

export class EditorAdapterError extends Error {}

type EditorMark = {
  type: string;
  attrs?: Record<string, unknown>;
};

type EditableSlot = {
  block: PostBlock;
  textKey: 'text' | 'caption';
  entitiesKey: 'entities' | 'caption_entities';
};

const CAPTION_BLOCKS = new Set([
  'photo',
  'video',
  'animation',
  'audio',
  'voice',
  'document',
]);

const INLINE_ENTITY_TYPES = new Set<TelegramEntity['type']>([
  'bold',
  'italic',
  'underline',
  'strikethrough',
  'code',
  'text_link',
]);

function editableSlot(document: PostDocument): EditableSlot {
  if (document.mode !== 'classic' || document.blocks.length !== 1) {
    throw new EditorAdapterError('Этот документ пока нельзя редактировать Classic Composer-ом.');
  }
  const block = document.blocks[0];
  if (block.type === 'text') {
    return { block, textKey: 'text', entitiesKey: 'entities' };
  }
  if (CAPTION_BLOCKS.has(block.type)) {
    return { block, textKey: 'caption', entitiesKey: 'caption_entities' };
  }
  throw new EditorAdapterError(`Формат ${block.type} пока открыт только для просмотра.`);
}

function telegramEntityFromMark(
  mark: EditorMark,
  offset: number,
  length: number,
): TelegramEntity | null {
  if (length <= 0) return null;
  switch (mark.type) {
    case 'bold':
      return { type: 'bold', offset, length };
    case 'italic':
      return { type: 'italic', offset, length };
    case 'underline':
      return { type: 'underline', offset, length };
    case 'strike':
      return { type: 'strikethrough', offset, length };
    case 'code':
      return { type: 'code', offset, length };
    case 'link': {
      const url = typeof mark.attrs?.href === 'string' ? mark.attrs.href : '';
      return url ? { type: 'text_link', offset, length, url } : null;
    }
    default:
      return null;
  }
}

function entityIdentity(entity: TelegramEntity): string {
  return `${entity.type}:${entity.url ?? ''}:${entity.language ?? ''}`;
}

function normalizeEntities(entities: TelegramEntity[], textLength: number): TelegramEntity[] {
  const cleaned = entities
    .map((entity) => {
      const offset = Math.max(0, Math.min(textLength, Math.trunc(entity.offset)));
      const end = Math.max(offset, Math.min(textLength, Math.trunc(entity.offset + entity.length)));
      return { ...entity, offset, length: end - offset };
    })
    .filter((entity) => entity.length > 0 && entity.offset < textLength)
    .sort((left, right) => left.offset - right.offset || right.length - left.length);

  const merged: TelegramEntity[] = [];
  for (const entity of cleaned) {
    const previous = merged.at(-1);
    if (
      previous &&
      entityIdentity(previous) === entityIdentity(entity) &&
      previous.offset + previous.length === entity.offset
    ) {
      previous.length += entity.length;
    } else {
      merged.push({ ...entity });
    }
  }
  return merged;
}

function editorJsonToTelegram(json: JSONContent): { text: string; entities: TelegramEntity[] } {
  let text = '';
  const entities: TelegramEntity[] = [];

  const append = (value: string, marks: EditorMark[] = []) => {
    if (!value) return;
    const start = text.length;
    text += value;
    const length = value.length;
    for (const mark of marks) {
      const entity = telegramEntityFromMark(mark, start, length);
      if (entity) entities.push(entity);
    }
  };

  const ensureNewline = () => {
    if (text && !text.endsWith('\n')) text += '\n';
  };

  const renderChildren = (children: JSONContent[] | undefined, separator = true) => {
    for (let index = 0; index < (children?.length ?? 0); index += 1) {
      if (separator && index > 0) ensureNewline();
      render(children![index]);
    }
  };

  const renderListItem = (node: JSONContent) => {
    renderChildren(node.content, true);
  };

  const render = (node: JSONContent): void => {
    switch (node.type) {
      case 'doc':
        renderChildren(node.content, true);
        return;
      case 'text':
        append(node.text ?? '', (node.marks ?? []) as EditorMark[]);
        return;
      case 'hardBreak':
        append('\n');
        return;
      case 'paragraph':
      case 'heading':
        renderChildren(node.content, false);
        return;
      case 'blockquote': {
        const start = text.length;
        renderChildren(node.content, true);
        const length = text.length - start;
        if (length > 0) entities.push({ type: 'blockquote', offset: start, length });
        return;
      }
      case 'codeBlock': {
        const start = text.length;
        renderChildren(node.content, false);
        const length = text.length - start;
        if (length > 0) {
          const language = typeof node.attrs?.language === 'string' ? node.attrs.language : undefined;
          entities.push({ type: 'pre', offset: start, length, ...(language ? { language } : {}) });
        }
        return;
      }
      case 'bulletList':
        (node.content ?? []).forEach((item, index) => {
          if (index > 0) ensureNewline();
          append('• ');
          renderListItem(item);
        });
        return;
      case 'orderedList': {
        const first = Number.isFinite(Number(node.attrs?.start)) ? Number(node.attrs?.start) : 1;
        (node.content ?? []).forEach((item, index) => {
          if (index > 0) ensureNewline();
          append(`${first + index}. `);
          renderListItem(item);
        });
        return;
      }
      case 'listItem':
        renderListItem(node);
        return;
      case 'horizontalRule':
        append('———');
        return;
      default:
        renderChildren(node.content, false);
    }
  };

  render(json);
  return { text, entities: normalizeEntities(entities, text.length) };
}

function markFromEntity(entity: TelegramEntity): EditorMark | null {
  switch (entity.type) {
    case 'bold':
      return { type: 'bold' };
    case 'italic':
      return { type: 'italic' };
    case 'underline':
      return { type: 'underline' };
    case 'strikethrough':
      return { type: 'strike' };
    case 'code':
      return { type: 'code' };
    case 'text_link':
      return entity.url ? { type: 'link', attrs: { href: entity.url } } : null;
    default:
      return null;
  }
}

function inlineNodes(
  value: string,
  absoluteOffset: number,
  entities: TelegramEntity[],
): JSONContent[] {
  if (!value) return [];
  const lineEnd = absoluteOffset + value.length;
  const relevant = entities.filter(
    (entity) =>
      INLINE_ENTITY_TYPES.has(entity.type) &&
      entity.offset < lineEnd &&
      entity.offset + entity.length > absoluteOffset,
  );
  const boundaries = new Set<number>([0, value.length]);
  for (const entity of relevant) {
    boundaries.add(Math.max(0, entity.offset - absoluteOffset));
    boundaries.add(Math.min(value.length, entity.offset + entity.length - absoluteOffset));
  }
  const sorted = [...boundaries].sort((a, b) => a - b);
  const nodes: JSONContent[] = [];
  for (let index = 0; index < sorted.length - 1; index += 1) {
    const start = sorted[index];
    const end = sorted[index + 1];
    if (end <= start) continue;
    const segment = value.slice(start, end);
    const absoluteStart = absoluteOffset + start;
    const absoluteEnd = absoluteOffset + end;
    const marks = relevant
      .filter(
        (entity) =>
          entity.offset <= absoluteStart && entity.offset + entity.length >= absoluteEnd,
      )
      .map(markFromEntity)
      .filter((mark): mark is EditorMark => mark !== null);
    nodes.push({ type: 'text', text: segment, ...(marks.length ? { marks } : {}) });
  }
  return nodes;
}

function fullLineEntity(
  type: 'blockquote' | 'pre',
  offset: number,
  length: number,
  entities: TelegramEntity[],
): TelegramEntity | undefined {
  return entities.find(
    (entity) => entity.type === type && entity.offset <= offset && entity.offset + entity.length >= offset + length,
  );
}

export function documentToEditorJson(document: PostDocument): JSONContent {
  const slot = editableSlot(document);
  const value = typeof slot.block[slot.textKey] === 'string' ? String(slot.block[slot.textKey]) : '';
  const rawEntities = slot.block[slot.entitiesKey];
  const entities = Array.isArray(rawEntities) ? rawEntities : [];

  const paragraphs: JSONContent[] = [];
  let cursor = 0;
  const lines = value.split('\n');
  lines.forEach((line, index) => {
    const content = inlineNodes(line, cursor, entities);
    const pre = fullLineEntity('pre', cursor, line.length, entities);
    const quote = fullLineEntity('blockquote', cursor, line.length, entities);
    let node: JSONContent = pre
      ? {
          type: 'codeBlock',
          attrs: { language: pre.language ?? null },
          content,
        }
      : { type: 'paragraph', content };
    if (quote && !pre) node = { type: 'blockquote', content: [node] };
    paragraphs.push(node);
    cursor += line.length + (index < lines.length - 1 ? 1 : 0);
  });

  return { type: 'doc', content: paragraphs.length ? paragraphs : [{ type: 'paragraph' }] };
}

export function editorJsonToDocument(json: JSONContent, base: PostDocument): PostDocument {
  const slot = editableSlot(base);
  const rendered = editorJsonToTelegram(json);
  const block = {
    ...slot.block,
    [slot.textKey]: rendered.text,
    [slot.entitiesKey]: rendered.entities,
  };
  return {
    ...base,
    blocks: [block],
  };
}

export function canEditDocument(document: PostDocument): boolean {
  try {
    editableSlot(document);
    return true;
  } catch {
    return false;
  }
}
