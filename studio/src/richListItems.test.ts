import { describe, expect, it } from 'vitest';

import {
  appendRichListItem,
  moveRichListItem,
  patchRichListItem,
  removeRichListItem,
  richListItemPreview,
  richListItems,
} from './richListItems';

describe('structured rich list items', () => {
  it('preserves renderer label and rich content semantics', () => {
    expect(richListItems([
      'Plain',
      { label: 'A.', content: [{ text: 'Bold', marks: ['bold'] }] },
      { label: 'B.', text: 'Legacy text alias' },
    ])).toEqual([
      'Plain',
      { label: 'A.', content: [{ text: 'Bold', marks: ['bold'] }] },
      { label: 'B.', content: 'Legacy text alias' },
    ]);
  });

  it('adds a label without flattening rich content', () => {
    expect(patchRichListItem(['First'], 0, { label: '1.' })).toEqual([
      { label: '1.', content: 'First' },
    ]);
    expect(patchRichListItem([
      { label: 'A.', content: [{ text: 'One', marks: ['italic'] }] },
    ], 0, { content: [{ text: 'Two', marks: ['bold'] }] })).toEqual([
      { label: 'A.', content: [{ text: 'Two', marks: ['bold'] }] },
    ]);
  });

  it('removes a blank label and keeps the compact string form when possible', () => {
    expect(patchRichListItem([{ label: '•', content: 'Text' }], 0, { label: '  ' })).toEqual(['Text']);
  });

  it('moves, appends, and removes without mutating structured items', () => {
    const source = [
      { label: 'A.', content: [{ text: 'One', marks: ['bold'] }] },
      'Two',
    ];
    const moved = moveRichListItem(source, 0, 1);
    expect(moved).toEqual([
      'Two',
      { label: 'A.', content: [{ text: 'One', marks: ['bold'] }] },
    ]);
    expect(source[0]).toEqual({ label: 'A.', content: [{ text: 'One', marks: ['bold'] }] });
    expect(appendRichListItem(source)).toHaveLength(3);
    expect(removeRichListItem(source, 0)).toEqual(['Two']);
    expect(removeRichListItem(['Only'], 0)).toEqual(['Only']);
  });

  it('builds preview text from rich segments instead of object stringification', () => {
    expect(richListItemPreview({
      label: 'A.',
      content: [
        { text: 'Rich ', marks: ['bold'] },
        { text: 'text', marks: ['italic'] },
      ],
    })).toEqual({ label: 'A.', text: 'Rich text' });
  });
});
