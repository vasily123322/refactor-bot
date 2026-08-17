import { describe, expect, it } from 'vitest';

import {
  canAuthorNestedBlocks,
  initialNestedBlocks,
  moveNestedBlock,
  nestedBlocksState,
  patchNestedBlock,
  removeNestedBlock,
} from './richNestedBlocks';

const source = [
  { id: 'a', type: 'paragraph', content: [{ text: 'A', marks: ['bold'] }] },
  { id: 'b', type: 'details', summary: 'B', blocks: [{ id: 'deep', type: 'paragraph', content: 'deep' }] },
];

describe('bounded nested rich blocks', () => {
  it('mirrors renderer fallback and rejects malformed non-empty child arrays', () => {
    expect(nestedBlocksState(undefined)).toEqual({ kind: 'none', blocks: [] });
    expect(nestedBlocksState({ ignored: true })).toEqual({ kind: 'none', blocks: [] });
    expect(nestedBlocksState([])).toEqual({ kind: 'none', blocks: [] });
    expect(nestedBlocksState(source).kind).toBe('editable');
    expect(nestedBlocksState([{ type: 'paragraph' }])).toEqual({
      kind: 'invalid',
      blocks: [{ type: 'paragraph' }],
    });
  });

  it('allows bounded authoring through depth two only', () => {
    expect(canAuthorNestedBlocks(0)).toBe(true);
    expect(canAuthorNestedBlocks(1)).toBe(true);
    expect(canAuthorNestedBlocks(2)).toBe(false);
    expect(canAuthorNestedBlocks(3)).toBe(false);
  });

  it('starts nested mode with one stable paragraph child', () => {
    expect(initialNestedBlocks('nested-1')).toEqual([
      { id: 'nested-1', type: 'paragraph', content: '' },
    ]);
  });

  it('patches and moves children without flattening deeper payloads', () => {
    const patched = patchNestedBlock(source, 0, { content: [{ text: 'Changed', marks: ['italic'] }] });
    expect(patched[0]).toEqual({ id: 'a', type: 'paragraph', content: [{ text: 'Changed', marks: ['italic'] }] });
    expect(patched[1]).toEqual(source[1]);

    const moved = moveNestedBlock(source, 1, -1);
    expect(moved[0]).toEqual(source[1]);
    expect(moved[0]).not.toBe(source[1]);
    expect(source[1]).toEqual({
      id: 'b',
      type: 'details',
      summary: 'B',
      blocks: [{ id: 'deep', type: 'paragraph', content: 'deep' }],
    });
  });

  it('never removes the last nested child', () => {
    expect(removeNestedBlock([source[0]], 0)).toEqual([source[0]]);
    expect(removeNestedBlock(source, 0)).toEqual([source[1]]);
  });
});
