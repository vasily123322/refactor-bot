import { describe, expect, it } from 'vitest';

import { richCaptionPatch, richCaptionPreview, richCaptionView } from './richCaption';

describe('rich media caption policy', () => {
  it('reads canonical sibling caption and credit as rich content', () => {
    expect(richCaptionView({
      caption: [{ text: 'Caption', marks: ['bold'] }],
      credit: [{ text: 'Source', marks: ['italic'] }],
    })).toEqual({
      text: [{ text: 'Caption', marks: ['bold'] }],
      credit: [{ text: 'Source', marks: ['italic'] }],
      present: true,
    });
  });

  it('normalizes renderer-supported caption mapping without data loss', () => {
    const source = {
      caption: {
        text: [{ text: 'Mapped', marks: ['underline'] }],
        credit: 'Author',
      },
    };
    expect(richCaptionView(source)).toEqual({
      text: [{ text: 'Mapped', marks: ['underline'] }],
      credit: 'Author',
      present: true,
    });
    expect(richCaptionPatch(source, { credit: 'Editor' })).toEqual({
      caption: [{ text: 'Mapped', marks: ['underline'] }],
      credit: 'Editor',
    });
  });

  it('keeps an empty caption when credit exists because renderer requires caption presence', () => {
    expect(richCaptionPatch({}, { credit: 'Photographer' })).toEqual({
      caption: '',
      credit: 'Photographer',
    });
  });

  it('removes the whole caption when both text and credit are blank', () => {
    expect(richCaptionPatch({ caption: 'Old', credit: 'Source' }, { text: ' ', credit: '' })).toEqual({
      caption: undefined,
      credit: undefined,
    });
  });

  it('builds fast-preview text from rich segments and credit', () => {
    expect(richCaptionPreview({
      caption: [{ text: 'Rich ', marks: ['bold'] }, { text: 'caption' }],
      credit: 'Credit',
    })).toEqual({ text: 'Rich caption', credit: 'Credit' });
    expect(richCaptionPreview({})).toBeNull();
  });
});
