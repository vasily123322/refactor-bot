import './rich-collections.css';

import { richCaptionPreview, type RichCaptionSource } from './richCaption';
import { richCollectionVisualModel } from './richCollections';
import { richListItems, richListItemPreview } from './richListItems';
import { richMediaVisualBadges } from './richMediaOptions';
import { canAuthorNestedBlocks, nestedBlocksState } from './richNestedBlocks';
import type { Channel, PostBlock, PostDocument, RichSegmentValue } from './types';
import { documentText } from './types';

function richText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return (value as RichSegmentValue[]).map((segment) => segment.text ?? '').join('');
  }
  return '';
}

function CaptionPreview({ source }: { source: RichCaptionSource }) {
  const caption = richCaptionPreview(source);
  if (!caption) return null;
  return (
    <p>
      {caption.text}
      {caption.credit ? <cite> — {caption.credit}</cite> : null}
    </p>
  );
}

function RichPreviewBlock({ block, depth = 0 }: { block: PostBlock; depth?: number }) {
  const collection = richCollectionVisualModel(block);
  if (collection) {
    const visibleItems = collection.items.slice(0, 6);
    return (
      <div className={`visual-rich-collection ${collection.type}`}>
        <div className="visual-rich-collection-head">
          <span>{collection.icon}</span>
          <strong>{collection.label}</strong>
          <small>{collection.items.length} медиа</small>
        </div>
        {visibleItems.length > 0 ? (
          <div className="visual-rich-collection-grid">
            {visibleItems.map((item, index) => {
              const badges = richMediaVisualBadges({
                id: `${block.id}:preview:${index}`,
                ...item,
              });
              const caption = richCaptionPreview(item);
              return (
                <div className="visual-rich-collection-item" key={`${item.asset_id}:${index}`}>
                  <strong>{item.kind}</strong>
                  <small>asset #{item.asset_id}</small>
                  {badges.length > 0 ? <small>{badges.join(' · ')}</small> : null}
                  {caption?.text ? <small>{caption.text}</small> : null}
                  {caption?.credit ? <small>— {caption.credit}</small> : null}
                </div>
              );
            })}
            {collection.items.length > visibleItems.length ? (
              <div className="visual-rich-collection-item more">
                +{collection.items.length - visibleItems.length}
              </div>
            ) : null}
          </div>
        ) : (
          <small className="visual-rich-collection-empty">media assets не выбраны</small>
        )}
        <CaptionPreview source={block} />
      </div>
    );
  }

  switch (block.type) {
    case 'paragraph':
      return <p className="visual-rich-paragraph">{richText(block.content)}</p>;
    case 'heading': {
      const size = Math.max(1, Math.min(6, Number(block.size) || 2));
      return <div className={`visual-rich-heading size-${size}`}>{richText(block.content)}</div>;
    }
    case 'divider':
      return <hr className="visual-rich-divider" />;
    case 'quote': {
      const nested = nestedBlocksState(block.blocks);
      return (
        <blockquote className="visual-rich-quote">
          {nested.kind === 'editable'
            ? canAuthorNestedBlocks(depth)
              ? nested.blocks.map((child) => (
                  <RichPreviewBlock key={`${block.id}:${child.id}`} block={child} depth={depth + 1} />
                ))
              : <small>{nested.blocks.length} deeper blocks · exact preview</small>
            : nested.kind === 'invalid'
              ? <small>Некорректные quote.blocks · exact preview</small>
              : richText(block.content)}
          {block.credit ? <cite>{richText(block.credit)}</cite> : null}
        </blockquote>
      );
    }
    case 'pull_quote':
      return (
        <blockquote className="visual-rich-pullquote">
          “{richText(block.content)}”
          {block.credit ? <cite>{richText(block.credit)}</cite> : null}
        </blockquote>
      );
    case 'list':
      return (
        <ul className="visual-rich-list">
          {richListItems(block.items).map((item, index) => {
            const preview = richListItemPreview(item);
            return (
              <li key={index}>
                {preview.label ? <strong>{preview.label} </strong> : null}
                {preview.text}
              </li>
            );
          })}
        </ul>
      );
    case 'details': {
      const nested = nestedBlocksState(block.blocks);
      return (
        <details className="visual-rich-details" open={Boolean(block.is_open)}>
          <summary>{richText(block.summary)}</summary>
          {nested.kind === 'editable'
            ? canAuthorNestedBlocks(depth)
              ? nested.blocks.map((child) => (
                  <RichPreviewBlock key={`${block.id}:${child.id}`} block={child} depth={depth + 1} />
                ))
              : <small>{nested.blocks.length} deeper blocks · exact preview</small>
            : nested.kind === 'invalid'
              ? <small>Некорректные details.blocks · exact preview</small>
              : <p>{richText(block.content)}</p>}
        </details>
      );
    }
    case 'math':
      return <div className="visual-rich-math">{String(block.formula ?? '')}</div>;
    case 'anchor':
      return <span className="visual-rich-anchor">#{String(block.name ?? '')}</span>;
    case 'image':
    case 'media': {
      const badges = richMediaVisualBadges(block);
      return (
        <div className="visual-rich-media">
          <div>
            <span>▣</span>
            <strong>{String(block.kind || (block.type === 'image' ? 'photo' : 'media'))}</strong>
            <small>{block.asset_id ? `asset #${String(block.asset_id)}` : 'asset не выбран'}</small>
            {badges.length > 0 ? <small>{badges.join(' · ')}</small> : null}
          </div>
          <CaptionPreview source={block} />
        </div>
      );
    }
    case 'map': {
      const latitude = Number(block.latitude ?? block.lat);
      const longitude = Number(block.longitude ?? block.lon ?? block.lng);
      const zoom = Number(block.zoom ?? 13);
      const coordinates = Number.isFinite(latitude) && Number.isFinite(longitude)
        ? `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`
        : 'координаты не заданы';
      return (
        <div className="visual-rich-map">
          <div>
            <span>⌖</span>
            <strong>Карта</strong>
            <small>zoom {Number.isFinite(zoom) ? zoom : '—'}</small>
          </div>
          <p>{coordinates}</p>
          <CaptionPreview source={block} />
        </div>
      );
    }
    default:
      return <div className="visual-rich-unsupported">{block.type}</div>;
  }
}

export function TelegramVisualPreview({
  document,
  channel,
}: {
  document: PostDocument;
  channel: Channel | null;
}) {
  const text = documentText(document) || 'Начните писать пост…';
  return (
    <div className="phone-stage" aria-label="Предпросмотр Telegram">
      <div className="phone-header">
        <span className="phone-back">‹</span>
        <div>
          <strong>{channel?.title || 'Telegram Channel'}</strong>
          <small>{document.mode === 'rich' ? 'Rich Message preview' : 'предпросмотр публикации'}</small>
        </div>
        <span>•••</span>
      </div>
      <div className="telegram-chat">
        <article className={document.mode === 'rich' ? 'telegram-post rich' : 'telegram-post'}>
          <div className="telegram-avatar">T</div>
          <div className="telegram-body">
            <strong className="telegram-author">{channel?.title || 'Канал'}</strong>
            {document.mode === 'rich' ? (
              <div className="telegram-rich-blocks">
                {document.blocks.map((block) => <RichPreviewBlock key={block.id} block={block} />)}
              </div>
            ) : (
              <div className="telegram-text">{text}</div>
            )}
            <div className="telegram-meta">сейчас · 👁 1</div>
          </div>
        </article>
      </div>
      <div className="preview-note">
        Visual preview помогает редактировать быстро. «В Telegram» отправляет exact preview через
        тот же renderer и Bot API, что production.
      </div>
    </div>
  );
}
