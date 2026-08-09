import { useEffect, useState } from 'react';

import { richMapDraft, richMapError, richMapPatch, type RichMapDraft } from './richMap';
import type { PostBlock } from './types';

function NumericField({
  label,
  value,
  step,
  onChange,
}: {
  label: string;
  value: number;
  step?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="rich-map-field">
      <span>{label}</span>
      <input
        type="number"
        value={Number.isFinite(value) ? value : ''}
        step={step ?? 1}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

export function MapBlockEditor({
  block,
  onPatch,
}: {
  block: PostBlock;
  onPatch: (patch: Partial<PostBlock>) => void;
}) {
  const [draft, setDraft] = useState<RichMapDraft>(() => richMapDraft(block));

  useEffect(() => {
    setDraft(richMapDraft(block));
  }, [block]);

  const error = richMapError(draft);
  const update = (patch: Partial<RichMapDraft>) => {
    const next = { ...draft, ...patch };
    setDraft(next);
    if (richMapError(next) === null) {
      onPatch(richMapPatch(next));
    }
  };

  return (
    <div className="rich-map-editor">
      <div className="rich-map-grid">
        <NumericField
          label="Широта"
          value={draft.latitude}
          step={0.000001}
          onChange={(latitude) => update({ latitude })}
        />
        <NumericField
          label="Долгота"
          value={draft.longitude}
          step={0.000001}
          onChange={(longitude) => update({ longitude })}
        />
        <NumericField
          label="Zoom"
          value={draft.zoom}
          onChange={(zoom) => update({ zoom })}
        />
        <NumericField
          label="Ширина"
          value={draft.width}
          onChange={(width) => update({ width })}
        />
        <NumericField
          label="Высота"
          value={draft.height}
          onChange={(height) => update({ height })}
        />
      </div>
      <label className="rich-field-label">
        <span>Caption</span>
        <textarea
          className="rich-media-caption"
          value={draft.caption}
          onChange={(event) => update({ caption: event.target.value })}
          placeholder="Подпись карты (необязательно)"
        />
      </label>
      {error && <small className="rich-media-warning">{error}</small>}
    </div>
  );
}
