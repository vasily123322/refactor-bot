import { describe, expect, it } from 'vitest';

import { DRAFT_AUTOSAVE_DELAY_MS, isCurrentDraftSave } from './draftAutosave';

const snapshot = { channelId: 7, contentId: 42, editVersion: 3 };

describe('draft autosave identity', () => {
  it('accepts only the exact current editor snapshot', () => {
    expect(isCurrentDraftSave(snapshot, {
      channelId: 7,
      contentId: 42,
      editVersion: 3,
    })).toBe(true);
  });

  it('rejects stale responses after edit or navigation', () => {
    expect(isCurrentDraftSave(snapshot, {
      channelId: 7,
      contentId: 42,
      editVersion: 4,
    })).toBe(false);
    expect(isCurrentDraftSave(snapshot, {
      channelId: 7,
      contentId: 43,
      editVersion: 3,
    })).toBe(false);
    expect(isCurrentDraftSave(snapshot, {
      channelId: 8,
      contentId: 42,
      editVersion: 3,
    })).toBe(false);
  });

  it('uses a deliberate debounce instead of per-keystroke revisions', () => {
    expect(DRAFT_AUTOSAVE_DELAY_MS).toBeGreaterThanOrEqual(1000);
  });
});
