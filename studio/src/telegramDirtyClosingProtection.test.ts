import { describe, expect, it, vi } from 'vitest';

import { createDirtyClosingProtectionController } from './dirtyClosingProtection';

function bridge(enabled = true) {
  return {
    enable: vi.fn(() => enabled),
    dispose: vi.fn(() => true),
  };
}

describe('dirty editor closing protection', () => {
  it('keeps a pristine editor unowned and does not register closing confirmation', () => {
    const provider = bridge();
    const controller = createDirtyClosingProtectionController(provider);
    expect(controller.sync(false)).toBe(true);
    expect(controller.ownsProtection()).toBe(false);
    expect(provider.enable).not.toHaveBeenCalled();
    expect(provider.dispose).not.toHaveBeenCalled();
  });

  it('enables once for dirty state and does not duplicate registration', () => {
    const provider = bridge();
    const controller = createDirtyClosingProtectionController(provider);
    expect(controller.sync(true)).toBe(true);
    expect(controller.sync(true)).toBe(true);
    expect(controller.ownsProtection()).toBe(true);
    expect(provider.enable).toHaveBeenCalledTimes(1);
  });

  it('releases protection after successful save makes the editor pristine', () => {
    const provider = bridge();
    const controller = createDirtyClosingProtectionController(provider);
    controller.sync(true);
    expect(controller.sync(false)).toBe(true);
    expect(controller.ownsProtection()).toBe(false);
    expect(provider.dispose).toHaveBeenCalledTimes(1);
  });

  it('keeps protection owned while a failed save leaves dirty=true', () => {
    const provider = bridge();
    const controller = createDirtyClosingProtectionController(provider);
    controller.sync(true);
    controller.sync(true);
    expect(controller.ownsProtection()).toBe(true);
    expect(provider.enable).toHaveBeenCalledTimes(1);
    expect(provider.dispose).not.toHaveBeenCalled();
  });

  it('component unmount releases owned protection and cleanup is idempotent', () => {
    const provider = bridge();
    const controller = createDirtyClosingProtectionController(provider);
    controller.sync(true);
    expect(controller.dispose()).toBe(true);
    expect(controller.dispose()).toBe(true);
    expect(provider.dispose).toHaveBeenCalledTimes(1);
  });

  it('React-style remount does not leave duplicate ownership or let stale cleanup release the new owner', () => {
    const provider = bridge();
    const firstMount = createDirtyClosingProtectionController(provider);
    firstMount.sync(true);
    firstMount.dispose();

    const secondMount = createDirtyClosingProtectionController(provider);
    secondMount.sync(true);
    firstMount.dispose();

    expect(provider.enable).toHaveBeenCalledTimes(2);
    expect(provider.dispose).toHaveBeenCalledTimes(1);
    expect(secondMount.ownsProtection()).toBe(true);

    secondMount.dispose();
    expect(provider.dispose).toHaveBeenCalledTimes(2);
  });

  it('unsupported Telegram clients fail safely and never claim lifecycle ownership', () => {
    const provider = bridge(false);
    const controller = createDirtyClosingProtectionController(provider);
    expect(controller.sync(true)).toBe(false);
    expect(controller.ownsProtection()).toBe(false);
    expect(controller.dispose()).toBe(true);
    expect(provider.dispose).not.toHaveBeenCalled();
  });

  it('keeps ownership when Telegram refuses disposal so protection is not falsely cleared', () => {
    const provider = {
      enable: vi.fn(() => true),
      dispose: vi.fn(() => false),
    };
    const controller = createDirtyClosingProtectionController(provider);
    controller.sync(true);
    expect(controller.sync(false)).toBe(false);
    expect(controller.ownsProtection()).toBe(true);
  });
});
