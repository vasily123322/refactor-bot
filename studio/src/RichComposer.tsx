import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  StudioApiError,
  studioApi,
  type MediaAssetKind,
  type MediaAssetView,
} from './api';
import { MapBlockEditor } from './MapBlockEditor';
import { MediaCollectionEditor } from './MediaCollectionEditor';
import { RichMediaOptionsEditor } from './RichMediaOptionsEditor';
import {
  isRichCollectionBlockType,
  newRichCollectionBlock,
  RICH_COLLECTION_BLOCK_OPTIONS,
} from './richCollections';
import { mediaAssetBlockPatch, mediaAssetOptionLabel } from './richMediaAssets';
import { richMediaAssetChangeCleanupPatch } from './richMediaOptions';
import { DEFAULT_RICH_MAP } from './richMap';
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
  ['media', '▣ Медиа'],
  ...RICH_COLLECTION_BLOCK_OPTIONS,
  ['map', '⌖ Карта'],
] as const;

const MEDIA_KINDS: Array<[MediaAssetKind, string]> = [
  ['photo', 'Фото'],
  ['video', 'Видео'],
  ['animation', 'Анимация'],
  ['audio', 'Аудио'],
  ['voice_note', 'Voice note'],
];

const MAX_MEDIA_UPLOAD_BYTES = 20 * 1024 * 1024;
type AssetTransport = 'upload' | 'https' | 'telegram';

function newId(prefix: string): string {
  const random = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2);
  return `${prefix}_${random}`;
}

function newBlock(type: string): PostBlock {
  const id = newId(type.slice(0, 3));
  const collection = newRichCollectionBlock(id, type);
  if (collection) return collection;
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
      return { id, type, formula: '' };
    case 'anchor':
      return { id, type, name: '' };
    case 'media':
      return { id, type, kind: 'photo', caption: '' };
    case 'map':
      return { id, type, ...DEFAULT_RICH_MAP };
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

function errorMessage(error: unknown): string {
  if (error instanceof StudioApiError || error instanceof Error) return error.message;
  return 'Неизвестная ошибка';
}

function mediaAccept(kind: MediaAssetKind): string {
  if (kind === 'photo') return 'image/*';
  if (kind === 'video') return 'video/*';
  if (kind === 'animation') return 'image/gif,video/mp4';
  return 'audio/*';
}

function BlockEditor({
  block,
  index,
  count,
  assets,
  onPatch,
  onMove,
  onDuplicate,
  onDelete,
}: {
  block: PostBlock;
  index: number;
  count: number;
  assets: MediaAssetView[];
  onPatch: (patch: Partial<PostBlock>) => void;
  onMove: (direction: -1 | 1) => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const title = BLOCK_OPTIONS.find(([type]) => type === block.type)?.[1] ?? block.type;
  const selectedAssetId = Number(block.asset_id || 0);
  const selectedAsset = assets.find((candidate) => candidate.id === selectedAssetId) ?? null;

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
          <label className="rich-field-label">
            <span>LaTeX expression</span>
            <textarea
              value={textValue(block.formula)}
              onChange={(event) => onPatch({ formula: event.target.value })}
              placeholder="E=mc^2"
            />
          </label>
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

        {block.type === 'media' && (
          <>
            <label className="rich-field-label">
              <span>Media asset</span>
              <select
                className="rich-media-select"
                value={selectedAssetId || ''}
                onChange={(event) => {
                  const assetId = Number(event.target.value || 0);
                  const asset = assets.find((candidate) => candidate.id === assetId) ?? null;
                  onPatch({
                    ...mediaAssetBlockPatch(asset),
                    ...richMediaAssetChangeCleanupPatch(asset?.kind ?? 'photo'),
                  });
                }}
              >
                <option value="">Выберите asset…</option>
                {assets.map((asset) => (
                  <option key={asset.id} value={asset.id}>{mediaAssetOptionLabel(asset)}</option>
                ))}
              </select>
            </label>
            <label className="rich-field-label">
              <span>Caption</span>
              <textarea
                className="rich-media-caption"
                value={textValue(block.caption)}
                onChange={(event) => onPatch({ caption: event.target.value })}
                placeholder="Подпись (необязательно)"
              />
            </label>
            <RichMediaOptionsEditor block={block} asset={selectedAsset} onPatch={onPatch} />
            {selectedAssetId === 0 && (
              <small className="rich-media-warning">Выберите asset перед exact preview / publish.</small>
            )}
          </>
        )}

        {isRichCollectionBlockType(block.type) && (
          <MediaCollectionEditor block={block} assets={assets} onPatch={onPatch} />
        )}

        {block.type === 'map' && (
          <MapBlockEditor block={block} onPatch={onPatch} />
        )}
      </div>
    </article>
  );
}

export function RichComposer({
  document,
  channelId,
  onChange,
}: {
  document: PostDocument;
  channelId: number | null;
  onChange: (document: PostDocument) => void;
}) {
  const blocks = useMemo(() => document.blocks, [document.blocks]);
  const [assets, setAssets] = useState<MediaAssetView[]>([]);
  const [assetError, setAssetError] = useState<string | null>(null);
  const [assetBusy, setAssetBusy] = useState(false);
  const [showAssetForm, setShowAssetForm] = useState(false);
  const [assetKind, setAssetKind] = useState<MediaAssetKind>('photo');
  const [assetTransport, setAssetTransport] = useState<AssetTransport>('upload');
  const [assetReference, setAssetReference] = useState('');
  const [assetFile, setAssetFile] = useState<File | null>(null);
  const [assetLabel, setAssetLabel] = useState('');

  const loadAssets = useCallback(async () => {
    if (channelId === null) {
      setAssets([]);
      return;
    }
    setAssetError(null);
    try {
      setAssets(await studioApi.mediaAssets(channelId));
    } catch (error) {
      setAssetError(errorMessage(error));
    }
  }, [channelId]);

  useEffect(() => {
    void loadAssets();
  }, [loadAssets]);

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

  const registerAsset = async () => {
    if (channelId === null) return;
    if (assetTransport === 'upload' && assetFile === null) return;
    if (assetTransport !== 'upload' && !assetReference.trim()) return;
    setAssetBusy(true);
    setAssetError(null);
    try {
      const label = assetLabel.trim() || null;
      const created = assetTransport === 'upload'
        ? await studioApi.uploadMediaAsset(channelId, assetKind, assetFile!, label)
        : await studioApi.createMediaAsset(channelId, {
            kind: assetKind,
            label,
            telegram_file_id: assetTransport === 'telegram' ? assetReference.trim() : null,
            storage_url: assetTransport === 'https' ? assetReference.trim() : null,
          });
      setAssets((current) => [created, ...current.filter((asset) => asset.id !== created.id)]);
      setAssetReference('');
      setAssetFile(null);
      setAssetLabel('');
      setShowAssetForm(false);
    } catch (error) {
      setAssetError(errorMessage(error));
    } finally {
      setAssetBusy(false);
    }
  };

  const canRegister = channelId !== null && !assetBusy && (
    assetTransport === 'upload' ? assetFile !== null : Boolean(assetReference.trim())
  );

  return (
    <div className="rich-composer">
      <div className="rich-composer-banner">
        <div>
          <strong>Telegram Rich Message</strong>
          <small>Структурные блоки Bot API · exact preview доступен через кнопку «В Telegram».</small>
        </div>
        <span>{blocks.length} блоков</span>
      </div>

      <div className="rich-media-library">
        <div className="rich-media-library-head">
          <div>
            <strong>Media assets</strong>
            <small>{assets.length} в текущем канале · raw URL/file ID не возвращаются API</small>
          </div>
          <div>
            <button onClick={() => void loadAssets()} disabled={assetBusy || channelId === null}>↻</button>
            <button onClick={() => setShowAssetForm((current) => !current)} disabled={channelId === null}>+ Asset</button>
          </div>
        </div>
        {assetError && <div className="rich-media-error">{assetError}</div>}
        {showAssetForm && (
          <div className="rich-media-register">
            <select value={assetKind} onChange={(event) => {
              setAssetKind(event.target.value as MediaAssetKind);
              setAssetFile(null);
            }}>
              {MEDIA_KINDS.map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}
            </select>
            <select value={assetTransport} onChange={(event) => {
              setAssetTransport(event.target.value as AssetTransport);
              setAssetReference('');
              setAssetFile(null);
            }}>
              <option value="upload">Загрузить файл</option>
              <option value="https">HTTPS URL</option>
              <option value="telegram">Telegram file ID</option>
            </select>
            {assetTransport === 'upload' ? (
              <label className="rich-media-file-picker">
                <input
                  type="file"
                  accept={mediaAccept(assetKind)}
                  onChange={(event) => {
                    const next = event.target.files?.[0] ?? null;
                    if (next && next.size > MAX_MEDIA_UPLOAD_BYTES) {
                      setAssetFile(null);
                      setAssetError('Файл превышает лимит 20 MiB');
                      event.currentTarget.value = '';
                      return;
                    }
                    setAssetError(null);
                    setAssetFile(next);
                  }}
                />
                <span>{assetFile ? `${assetFile.name} · ${Math.ceil(assetFile.size / 1024)} KiB` : 'До 20 MiB'}</span>
              </label>
            ) : (
              <input
                value={assetReference}
                onChange={(event) => setAssetReference(event.target.value)}
                placeholder={assetTransport === 'https' ? 'https://cdn.example.com/media…' : 'Telegram file ID…'}
              />
            )}
            <input
              value={assetLabel}
              onChange={(event) => setAssetLabel(event.target.value)}
              placeholder="Название (необязательно)"
            />
            <button onClick={() => void registerAsset()} disabled={!canRegister}>
              {assetBusy ? 'Сохраняю…' : assetTransport === 'upload' ? 'Загрузить' : 'Добавить'}
            </button>
          </div>
        )}
      </div>

      <div className="rich-block-list">
        {blocks.map((block, index) => (
          <BlockEditor
            key={block.id}
            block={block}
            index={index}
            count={blocks.length}
            assets={assets}
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
