import { describe, expect, it } from 'vitest';

import {
  isEditorDocumentDirty,
  postDocumentsEqual,
  reconcileSuccessfulDraftSave,
} from './editorDirty';
import type { PostDocument } from './types';

function document(text: string): PostDocument {
  return {
    schema_version: 1,
    mode: 'classic',
    blocks: [{ id: 'b1', type: 'text', text, entities: [] }],
    telegram: {},
    metadata: {},
  };
}

describe('editor dirty authority', () => {
  it('treats an untouched or structurally cloned document as pristine', () => {
    const baseline = document('hello');
    expect(isEditorDocumentDirty(baseline, structuredClone(baseline))).toBe(false);
  });

  it('detects content edits and clears dirty after exact revert', () => {
    const baseline = document('hello');
    expect(isEditorDocumentDirty(baseline, document('changed'))).toBe(true);
    expect(isEditorDocumentDirty(baseline, document('hello'))).toBe(false);
  });

  it('uses JSON-semantic object equality rather than object insertion order', () => {
    const left = document('hello');
    left.metadata = { alpha: 1, beta: { enabled: true } };
    const right = document('hello');
    right.metadata = { beta: { enabled: true }, alpha: 1 };
    expect(postDocumentsEqual(left, right)).toBe(true);
  });

  it('treats missing and undefined object properties equivalently for JSON state', () => {
    const left = document('hello');
    left.metadata = { optional: undefined };
    const right = document('hello');
    expect(postDocumentsEqual(left, right)).toBe(true);
  });

  it('advances the canonical baseline after a successful save and preserves newer dirty edits', () => {
    const savedByBackend = document('snapshot A');
    const newerEditorState = document('snapshot B');
    expect(reconcileSuccessfulDraftSave(savedByBackend, newerEditorState)).toEqual({
      baseline: savedByBackend,
      dirty: true,
    });
  });

  it('clears dirty when the backend-saved document is still the current editor state', () => {
    const savedByBackend = document('saved');
    expect(reconcileSuccessfulDraftSave(savedByBackend, structuredClone(savedByBackend))).toEqual({
      baseline: savedByBackend,
      dirty: false,
    });
  });

  it('ignores unrelated UX preference changes because dirty authority compares documents only', () => {
    const baseline = document('hello');
    const current = structuredClone(baseline);
    const preferences = { sourceCreateKind: 'rss' };
    preferences.sourceCreateKind = 'telegram';
    expect(isEditorDocumentDirty(baseline, current)).toBe(false);
  });
});
