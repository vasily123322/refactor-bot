import { useMemo } from 'react';

import { RichTextField } from './RichTextField';
import type { PostBlock, PostDocument, RichSegmentValue } from './types';

const BLOCK_OPTIONS = [
  ['paragraph', '¶ Абзац'],
  ['heading', 'H Заголовок'],
  ['quote', '❞ Цитата'],
  ['pull_quote', '“ Pull quote'],
  ['list', '• Список'],
  ['details', '▸ Details'],
  ['divider', '— Разделитель'],
  ['math', '∑ Формула'],
  ['anchor', '# Anchor'],
] as const;

function newId(prefix: string): string {
  const random = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2);
  return `${prefix}_${random}`;
}

function newBlock(type: string): PostBlock {
  const id = newId(type.slice(0, 3));
  switch (type) {
    case 'heading':
      return { id, type, size: 2, content: '' };
    case 'quote':
      return { id, type, content: '', credit: '' };
    case 'pull_quote':
      return { id, type, content: '', credit: '' };
    case 'list':
      return { id, type, items: ['Новый пункт'] };
    case 'details':
      return { id, type, summary: 'Подробнее', content: '', is_open: false };
    case 'divider':
      return { id, type };
    case 'math':
      return { id, type, formula: '', size: 1 };
    case 'anchor':
      return { id, type, name: '' };
    default:
      return { id, type: 'paragraph', content: '' };
  }
}

function richValue(value: unknown): string | RichSegmentValue[] {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value as RichSegmentValue[];
  return '';
}

function textValue(value: unknown): string {
  return value === null || value === undefined ? '' : String(value);
}

function numberValue(value: unknown, fallback: number): number {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function BlockEditor({
  block,
  index,
  count,
  onPatch,
  onMove,
  onDuplicate,
  onDelete,
}: {
  block: PostBlock;
  index: number;
  count: number;
  onPatch: (patch: Partial<PostBlock>) => void;
  onMove: (direction: -1 | 1) => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const title = BLOCK_OPTIONS.find(([type]) => type === block.type)?.[1] ?? block.type;

  return (
    <article className={`rich-block rich-block-${block.type}`}>
      <header className="rich-block-header">
        <span className="rich-block-grip">⋮⋮</span>
        <strong>{title}</strong>
        <code>{block.id}</code>
        <span className="rich-block-spacer" />
        <button disabled={index === 0} onClick={() => onMove(-1)} title="Выше">↑</button>
        <button disabled={index === count - 1} onClick={() => onMove(1)} title="Ниже">↓</button>
        <button onClick={onDuplicate} title="Дублировать">⧉</button>
        <button onClick={onDelete} title="Удалить">×</button>
      </header>

      <div className="rich-block-body">
        {block.type === 'paragraph' && (
          <RichTextField
            value={richValue(block.content)}
            onChange={(content) => onPatch({ content })}
            placeholder="Абзац…"
          />
        )}

        {block.type === 'heading' && (
          <>
            <label className="rich-control-row">
              <span>Размер</span>
              <select
                value={numberValue(block.size, 2)}
                onChange={(event) => onPatch({ size: Number(event.target.value) })}
              >
                {[1, 2, 3, 4, 5, 6].map((size) => <option key={size} value={size}>H{size}</option>)}
              </select>
            </label>
            <RichTextField
              value={richValue(block.content)}
              onChange={(content) => onPatch({ content })}
              placeholder="Заголовок…"
            />
          </>
        )}

        {(block.type === 'quote' || block.type === 'pull_quote') && (
          <>
            <RichTextField
              value={richValue(block.content)}
              onChange={(content) => onPatch({ content })}
              placeholder="Текст цитаты…"
            />
            <RichTextField
              value={richValue(block.credit)}
              onChange={(credit) => onPatch({ credit })}
              placeholder="Автор / источник (необязательно)"
              compact
            />
          </>
        )}

        {block.type === 'list' && (
          <label className="rich-field-label">
            <span>Один пункт на строку</span>
            <textarea
              className="rich-list-input"
              value={
                Array.isArray(block.items)
                  ? block.items.map((item) =>
                      typeof item === 'string'
                        ? item
                        : typeof item === 'object' && item !== null
                          ? textValue((item as { content?: unknown; text?: unknown }).content ?? (item as { text?: unknown }).text)
                          : textValue(item),
                    ).join('\n')
                  : ''
              }
              onChange={(event) =>
                onPatch({
                  items: event.target.value.split('\n').filter((value) => value.length > 0),
                })
              }
              placeholder="Первый пункт\nВторой пункт"
            />
          </label>
        )}

        {block.type === 'details' && (
          <>
            <RichTextField
              value={richValue(block.summary)}
              onChange={(summary) => onPatch({ summary })}
              placeholder="Заголовок раскрывающегося блока…"
              compact
            />
            <RichTextField
              value={richValue(block.content)}
              onChange={(content) => onPatch({ content })}
              placeholder="Содержимое details…"
            />
            <label className="rich-check-row">
              <input
                type="checkbox"
                checked={Boolean(block.is_open)}
                onChange={(event) => onPatch({ is_open: event.target.checked })}
              />
              <span>Открывать блок раскрытым</span>
            </label>
          </>
        )}

        {block.type === 'divider' && <div className="rich-divider-preview" />}

        {block.type === 'math' && (
          <div className="rich-math-grid">
            <label>
              <span>Формула</span>
              <textarea
                value={textValue(block.formula)}
                onChange={(event) => onPatch({ formula: event.target.value })}
                placeholder="E=mc^2"
              />
            </label>
            <label>
              <span>Размер</span>
              <select
                value={numberValue(block.size, 1)}
                onChange={(event) => onPatch({ size: Number(event.target.value) })}
              >
                {[1, 2, 3, 4, 5, 6].map((size) => <option key={size} value={size}>{size}</option>)}
              </select>
            </label>
          </div>
        )}

        {block.type === 'anchor' && (
          <label className="rich-field-label">
            <span>Имя anchor</span>
            <input
              value={textValue(block.name)}
              onChange={(event) => onPatch({ name: event.target.value })}
              placeholder="section-one"
            />
          </label>
        )}
      </div>
    </article>
  );
}

export function RichComposer({
  document,
  onChange,
}: {
  document: PostDocument;
  onChange: (document: PostDocument) => void;
}) {
  const blocks = useMemo(() => document.blocks, [document.blocks]);

  const replaceBlocks = (next: PostBlock[]) => onChange({ ...document, blocks: next });
  const patch = (index: number, value: Partial<PostBlock>) => {
    replaceBlocks(blocks.map((block, current) => current === index ? { ...block, ...value } : block));
  };
  const move = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= blocks.length) return;
    const next = [...blocks];
    [next[index], next[target]] = [next[target], next[index]];
    replaceBlocks(next);
  };
  const duplicate = (index: number) => {
    const original = blocks[index];
    const clone = structuredClone(original);
    clone.id = newId(original.type.slice(0, 3));
    replaceBlocks([...blocks.slice(0, index + 1), clone, ...blocks.slice(index + 1)]);
  };
  const remove = (index: number) => {
    if (blocks.length <= 1) {
      replaceBlocks([newBlock('paragraph')]);
      return;
    }
    replaceBlocks(blocks.filter((_, current) => current !== index));
  };
  const add = (type: string) => replaceBlocks([...blocks, newBlock(type)]);

  return (
    <div className="rich-composer">
      <div className="rich-composer-banner">
        <div>
          <strong>Telegram Rich Message</strong>
          <small>Структурные блоки Bot API · exact preview доступен через кнопку «В Telegram».</small>
        </div>
        <span>{blocks.length} блоков</span>
      </div>

      <div className="rich-block-list">
        {blocks.map((block, index) => (
          <BlockEditor
            key={block.id}
            block={block}
            index={index}
            count={blocks.length}
            onPatch={(value) => patch(index, value)}
            onMove={(direction) => move(index, direction)}
            onDuplicate={() => duplicate(index)}
            onDelete={() => remove(index)}
          />
        ))}
      </div>

      <div className="rich-add-block">
        <span>Добавить блок</span>
        <div>
          {BLOCK_OPTIONS.map(([type, label]) => (
            <button key={type} onClick={() => add(type)}>{label}</button>
          ))}
        </div>
      </div>
    </div>
  );
}
