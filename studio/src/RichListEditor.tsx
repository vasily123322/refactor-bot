import './rich-list-editor.css';

import {
  appendRichListItem,
  moveRichListItem,
  patchRichListItem,
  removeRichListItem,
  richListItems,
  richListItemView,
} from './richListItems';
import { RichTextField } from './RichTextField';
import type { PostBlock } from './types';

export function RichListEditor({
  block,
  onPatch,
}: {
  block: PostBlock;
  onPatch: (patch: Partial<PostBlock>) => void;
}) {
  const items = richListItems(block.items);

  return (
    <div className="rich-list-editor">
      {items.map((item, index) => {
        const view = richListItemView(item);
        return (
          <article className="rich-list-item-card" key={`${block.id}:list:${index}`}>
            <header className="rich-list-item-head">
              <strong>Пункт {index + 1}</strong>
              <span className="rich-block-spacer" />
              <button
                disabled={index === 0}
                onClick={() => onPatch({ items: moveRichListItem(items, index, -1) })}
                title="Выше"
              >↑</button>
              <button
                disabled={index === items.length - 1}
                onClick={() => onPatch({ items: moveRichListItem(items, index, 1) })}
                title="Ниже"
              >↓</button>
              <button
                disabled={items.length <= 1}
                onClick={() => onPatch({ items: removeRichListItem(items, index) })}
                title="Удалить"
              >×</button>
            </header>
            <label className="rich-list-label-field">
              <span>Label (необязательно)</span>
              <input
                value={view.label}
                onChange={(event) => onPatch({
                  items: patchRichListItem(items, index, { label: event.target.value }),
                })}
                placeholder="A. / 1. / →"
              />
            </label>
            <RichTextField
              value={view.content}
              onChange={(content) => onPatch({
                items: patchRichListItem(items, index, { content }),
              })}
              placeholder="Текст пункта…"
              compact
            />
          </article>
        );
      })}

      {items.length === 0 && (
        <small className="rich-media-warning">Список должен содержать хотя бы один пункт.</small>
      )}
      <button
        className="rich-list-add"
        onClick={() => onPatch({ items: appendRichListItem(items) })}
      >+ Пункт</button>
    </div>
  );
}
