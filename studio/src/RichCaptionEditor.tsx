import { richCaptionPatch, richCaptionView, type RichCaptionSource } from './richCaption';
import { RichTextField } from './RichTextField';
import type { PostBlock } from './types';

export function RichCaptionEditor({
  source,
  onPatch,
  captionPlaceholder = 'Подпись (необязательно)',
}: {
  source: RichCaptionSource;
  onPatch: (patch: Partial<PostBlock>) => void;
  captionPlaceholder?: string;
}) {
  const caption = richCaptionView(source);
  return (
    <div className="rich-caption-editor">
      <div className="rich-field-label">
        <span>Caption</span>
        <RichTextField
          value={caption.text}
          onChange={(text) => onPatch(richCaptionPatch(source, { text }))}
          placeholder={captionPlaceholder}
          compact
        />
      </div>
      <div className="rich-field-label">
        <span>Credit / источник</span>
        <RichTextField
          value={caption.credit}
          onChange={(credit) => onPatch(richCaptionPatch(source, { credit }))}
          placeholder="Источник (необязательно)"
          compact
        />
      </div>
    </div>
  );
}
