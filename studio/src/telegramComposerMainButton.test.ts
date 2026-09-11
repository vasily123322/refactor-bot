import { describe, expect, it, vi } from 'vitest';

import { bindTelegramComposerMainButton } from './telegramComposerMainButton';

describe('Composer Telegram MainButton mirror', () => {
  it('does not claim MainButton outside the active Composer surface', () => {
    const bind = vi.fn(() => vi.fn());

    const dispose = bindTelegramComposerMainButton({
      active: false,
      label: '👁 В Telegram',
      disabled: false,
      loading: false,
      onSubmit: vi.fn(),
    }, bind);

    expect(dispose).toBeNull();
    expect(bind).not.toHaveBeenCalled();
  });

  it('projects the canonical label, eligibility, pending state, and handler', () => {
    const submit = vi.fn();
    const release = vi.fn();
    const bind = vi.fn(() => release);

    const dispose = bindTelegramComposerMainButton({
      active: true,
      label: '👁 В Telegram',
      disabled: true,
      loading: true,
      onSubmit: submit,
    }, bind);

    expect(dispose).toBe(release);
    expect(bind).toHaveBeenCalledTimes(1);
    expect(bind).toHaveBeenCalledWith(
      '👁 В Telegram',
      submit,
      { enabled: false, loading: true },
    );
  });

  it('keeps the native surface enabled when the canonical action is enabled', () => {
    const submit = vi.fn();
    const bind = vi.fn(() => vi.fn());

    bindTelegramComposerMainButton({
      active: true,
      label: '👁 В Telegram',
      disabled: false,
      loading: false,
      onSubmit: submit,
    }, bind);

    expect(bind).toHaveBeenCalledWith(
      '👁 В Telegram',
      submit,
      { enabled: true, loading: false },
    );
  });

  it('fails open to the HTML fallback when the native bridge is unavailable', () => {
    const bind = vi.fn(() => null);

    expect(bindTelegramComposerMainButton({
      active: true,
      label: '👁 В Telegram',
      disabled: false,
      loading: false,
      onSubmit: vi.fn(),
    }, bind)).toBeNull();
  });
});
