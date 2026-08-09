import type { Channel, PostBlock, PostDocument, RichSegmentValue } from './types';
import { documentText } from './types';

function richText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return (value as RichSegmentValue[]).map((segment) => segment.text ?? '').join('');
  }
  return '';
}

function RichPreviewBlock({ block }: { block: PostBlock }) {
  switch (block.type) {
    case 'paragraph':
      return <p className="visual-rich-paragraph">{richText(block.content)}</p>;
    case 'heading': {
      const size = Math.max(1, Math.min(6, Number(block.size) || 2));
      return <div className={`visual-rich-heading size-${size}`}>{richText(block.content)}</div>;
    }
    case 'divider':
      return <hr className="visual-rich-divider" />;
    case 'quote':
      return (
        <blockquote className="visual-rich-quote">
          {richText(block.content)}
          {block.credit ? <cite>{richText(block.credit)}</cite> : null}
        </blockquote>
      );
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
          {(Array.isArray(block.items) ? block.items : []).map((item, index) => (
            <li key={index}>
              {typeof item === 'string'
                ? item
                : typeof item === 'object' && item !== null
                  ? String((item as { content?: unknown; text?: unknown }).content ?? (item as { text?: unknown }).text ?? '')
                  : String(item)}
            </li>
          ))}
        </ul>
      );
    case 'details':
      return (
        <details className="visual-rich-details" open={Boolean(block.is_open)}>
          <summary>{richText(block.summary)}</summary>
          <p>{richText(block.content)}</p>
        </details>
      );
    case 'math':
      return <div className="visual-rich-math">{String(block.formula ?? '')}</div>;
    case 'anchor':
      return <span className="visual-rich-anchor">#{String(block.name ?? '')}</span>;
    case 'image':
    case 'media':
      return (
        <div className="visual-rich-media">
          <div>
            <span>▣</span>
            <strong>{String(block.kind || (block.type === 'image' ? 'photo' : 'media'))}</strong>
            <small>{block.asset_id ? `asset #${String(block.asset_id)}` : 'asset не выбран'}</small>
          </div>
          {block.caption ? <p>{richText(block.caption)}</p> : null}
        </div>
      );
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
