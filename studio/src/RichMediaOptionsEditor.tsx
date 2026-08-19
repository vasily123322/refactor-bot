import './rich-media-options.css';

import type { MediaAssetView } from './api';
import {
  boundedOptionalInteger,
  optionalText,
  richMediaKind,
  richMediaOptionCapabilities,
} from './richMediaOptions';
import type { PostBlock } from './types';

type NumericOptionField = 'width' | 'height' | 'duration';

function blockNumber(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string' && value.trim() === '') return '';
  const parsed = Number(value);
  return Number.isInteger(parsed) ? String(parsed) : '';
}

function OptionalIntegerField({
  label,
  field,
  value,
  fallback,
  minimum,
  maximum,
  onPatch,
}: {
  label: string;
  field: NumericOptionField;
  value: unknown;
  fallback: number | null | undefined;
  minimum: number;
  maximum: number;
  onPatch: (patch: Partial<PostBlock>) => void;
}) {
  return (
    <label className="rich-media-option-field">
      <span>{label}</span>
      <input
        type="number"
        min={minimum}
        max={maximum}
        step={1}
        value={blockNumber(value)}
        placeholder={fallback === null || fallback === undefined ? 'auto' : String(fallback)}
        onChange={(event) => {
          const parsed = boundedOptionalInteger(event.target.value, { minimum, maximum });
          if (parsed !== null) onPatch({ [field]: parsed });
        }}
      />
    </label>
  );
}

export function RichMediaOptionsEditor({
  block,
  asset,
  onPatch,
}: {
  block: PostBlock;
  asset: MediaAssetView | null;
  onPatch: (patch: Partial<PostBlock>) => void;
}) {
  const kind = richMediaKind(block.kind);
  const capabilities = richMediaOptionCapabilities(kind);

  return (
    <div className="rich-media-options">
      {(capabilities.spoiler || capabilities.streaming) && (
        <div className="rich-media-option-checks">
          {capabilities.spoiler && (
            <label className="rich-check-row">
              <input
                type="checkbox"
                checked={block.has_spoiler === true}
                onChange={(event) => onPatch({ has_spoiler: event.target.checked })}
              />
              <span>Spoiler</span>
            </label>
          )}
          {capabilities.streaming && (
            <label className="rich-check-row">
              <input
                type="checkbox"
                checked={block.supports_streaming === true}
                onChange={(event) => onPatch({ supports_streaming: event.target.checked })}
              />
              <span>Streaming video</span>
            </label>
          )}
        </div>
      )}

      {(capabilities.dimensions || capabilities.duration) && (
        <div className="rich-media-option-grid">
          {capabilities.dimensions && (
            <>
              <OptionalIntegerField
                label="Width override"
                field="width"
                value={block.width}
                fallback={asset?.width}
                minimum={1}
                maximum={10000}
                onPatch={onPatch}
              />
              <OptionalIntegerField
                label="Height override"
                field="height"
                value={block.height}
                fallback={asset?.height}
                minimum={1}
                maximum={10000}
                onPatch={onPatch}
              />
            </>
          )}
          {capabilities.duration && (
            <OptionalIntegerField
              label="Duration override, sec"
              field="duration"
              value={block.duration}
              fallback={asset?.duration_seconds}
              minimum={0}
              maximum={86400}
              onPatch={onPatch}
            />
          )}
        </div>
      )}

      {capabilities.audioMetadata && (
        <div className="rich-media-option-grid">
          <label className="rich-media-option-field">
            <span>Performer</span>
            <input
              value={typeof block.performer === 'string' ? block.performer : ''}
              onChange={(event) => onPatch({ performer: optionalText(event.target.value) })}
              placeholder="Исполнитель (необязательно)"
            />
          </label>
          <label className="rich-media-option-field">
            <span>Title</span>
            <input
              value={typeof block.title === 'string' ? block.title : ''}
              onChange={(event) => onPatch({ title: optionalText(event.target.value) })}
              placeholder="Название трека (необязательно)"
            />
          </label>
        </div>
      )}

      {(capabilities.dimensions || capabilities.duration) && (
        <small className="rich-media-option-note">
          Пустой override использует metadata выбранного asset на server-side resolver.
        </small>
      )}
    </div>
  );
}
