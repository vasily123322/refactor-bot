import { describe, expect, it } from 'vitest';

import { studioHapticNotificationType } from './studioHapticsPolicy';

describe('Studio haptics policy', () => {
  it('uses success feedback only for explicit successful save/publish outcomes', () => {
    expect(studioHapticNotificationType('manual-save-success')).toBe('success');
    expect(studioHapticNotificationType('publish-success')).toBe('success');
  });

  it('uses warning feedback for destructive confirmation', () => {
    expect(studioHapticNotificationType('destructive-confirmation')).toBe('warning');
  });

  it('uses error feedback for explicit user-facing failures and validation errors', () => {
    expect(studioHapticNotificationType('manual-save-error')).toBe('error');
    expect(studioHapticNotificationType('publish-error')).toBe('error');
    expect(studioHapticNotificationType('validation-error')).toBe('error');
    expect(studioHapticNotificationType('action-error')).toBe('error');
  });
});
