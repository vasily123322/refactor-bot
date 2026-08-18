import { describe, expect, it, vi } from 'vitest';

import {
  bindTelegramSourceCreateSecondaryButton,
  SOURCE_CREATE_CANCEL_LABEL,
} from './telegramSourceCreateSecondaryButton';

describe('Source create Telegram SecondaryButton mirror', () => {
  it('does not claim SecondaryButton while the create form is closed', () => {
    const bind = vi.fn(() => vi.fn());

    expect(bindTelegramSourceCreateSecondaryButton({
      active: false,
      onCancel: vi.fn(),
    }, bind)).toBeNull();
    expect(bind).not.toHaveBeenCalled();
  });

  it('projects the canonical cancel label and exact handler while the form is open', () => {
    const cancel = vi.fn();
    const release = vi.fn();
    const bind = vi.fn(() => release);

    const dispose = bindTelegramSourceCreateSecondaryButton({
      active: true,
      onCancel: cancel,
    }, bind);

    expect(dispose).toBe(release);
    expect(bind).toHaveBeenCalledTimes(1);
    expect(bind).toHaveBeenCalledWith(SOURCE_CREATE_CANCEL_LABEL, cancel, 'left');
  });

  it('fails open to the HTML cancel control when native SecondaryButton is unavailable', () => {
    const bind = vi.fn(() => null);

    expect(bindTelegramSourceCreateSecondaryButton({
      active: true,
      onCancel: vi.fn(),
    }, bind)).toBeNull();
  });
});
