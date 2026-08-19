import './rich-media-collection-options.css';

import { useState } from 'react';

import type { MediaAssetView } from './api';
import { RichCaptionEditor } from './RichCaptionEditor';
import { RichMediaOptionsEditor } from './RichMediaOptionsEditor';
import {
  mediaAssetOptionLabel,
  mediaCollectionItem,
  mediaCollectionItems,
  mediaCollectionItemWithAsset,
  moveMediaCollectionItem,
  patchMediaCollectionItem,
} from './richMediaAssets';
import type { PostBlock } from './types';

export function MediaCollectionEditor({
  block,
  assets,
  onPatch,
}: {
  block: PostBlock;
  assets: MediaAssetView[];
  onPatch: (patch: Partial<PostBlock>) => void;
}) {
  const [pendingAssetId, setPendingAssetId] = useState('');
  const items = mediaCollectionItems(block.items);

  const replaceItem = (index: number, assetId: number) => {
    const asset = assets.find((candidate) => candidate.id === assetId);
    if (!asset) return;
    onPatch({
      items: items.map((item, current) =>
        current === index ? mediaCollectionItemWithAsset(item, asset) : item,
      ),
    });
  };

  const patchItem = (index: number, value: Partial<PostBlock>) => {
    onPatch({
      items: items.map((item, current) =>
        current === index ? patchMediaCollectionItem(item, value) : item,
      ),
    });
  };

  const addItem = () => {
    const assetId = Number(pendingAssetId || 0);
    const asset = assets.find((candidate) => candidate.id === assetId);
    if (!asset) return;
    onPatch({ items: [...items, mediaCollectionItem(asset)] });
    setPendingAssetId('');
  };

  return (
    <div className="rich-media-collection-editor">
      <div className="rich-media-collection-items">
        {items.map((item, index) => {
          const asset = assets.find((candidate) => candidate.id === item.asset_id) ?? null;
          const itemBlock: PostBlock = { id: `${block.id}:item:${index}`, ...item };
          return (
            <div className="rich-media-collection-card" key={`${item.asset_id}:${index}`}>
              <div className="rich-media-collection-row">
                <span className="rich-media-collection-index">{index + 1}</span>
                <select
                  value={item.asset_id}
                  onChange={(event) => replaceItem(index, Number(event.target.value))}
                >
                  {assets.map((candidate) => (
                    <option key={candidate.id} value={candidate.id}>{mediaAssetOptionLabel(candidate)}</option>
                  ))}
                </select>
                <button
                  disabled={index === 0}
                  onClick={() => onPatch({ items: moveMediaCollectionItem(items, index, -1) })}
                  title="Выше"
                >↑</button>
                <button
                  disabled={index === items.length - 1}
                  onClick={() => onPatch({ items: moveMediaCollectionItem(items, index, 1) })}
                  title="Ниже"
                >↓</button>
                <button
                  onClick={() => onPatch({ items: items.filter((_, current) => current !== index) })}
                  title="Удалить"
                >×</button>
              </div>
              <details className="rich-media-collection-options">
                <summary>Параметры media item</summary>
                <RichCaptionEditor
                  source={itemBlock}
                  onPatch={(value) => patchItem(index, value)}
                  captionPlaceholder="Подпись media item (необязательно)"
                />
                <RichMediaOptionsEditor
                  block={itemBlock}
                  asset={asset}
                  onPatch={(value) => patchItem(index, value)}
                />
              </details>
            </div>
          );
        })}
      </div>

      <div className="rich-media-collection-add">
        <select value={pendingAssetId} onChange={(event) => setPendingAssetId(event.target.value)}>
          <option value="">Добавить asset…</option>
          {assets.map((asset) => (
            <option key={asset.id} value={asset.id}>{mediaAssetOptionLabel(asset)}</option>
          ))}
        </select>
        <button disabled={!pendingAssetId} onClick={addItem}>Добавить</button>
      </div>

      <RichCaptionEditor
        source={block}
        onPatch={onPatch}
        captionPlaceholder="Подпись коллекции (необязательно)"
      />

      {items.length === 0 && (
        <small className="rich-media-warning">Добавьте хотя бы один asset перед exact preview / publish.</small>
      )}
    </div>
  );
}
