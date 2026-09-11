import { describe, expect, it, vi } from 'vitest';

import { createTelegramButtonBridge } from './telegramButtons';

function availableSync<Args extends unknown[], Value>(
  implementation: (...args: Args) => Value,
) {
  return Object.assign(vi.fn(implementation), {
    isAvailable: vi.fn(() => true),
  });
}

function buttonProvider() {
  let mounted = false;
  const removers: ReturnType<typeof vi.fn>[] = [];
  const provider = {
    isSupported: vi.fn(() => true),
    isMounted: vi.fn(() => mounted),
    mount: availableSync(() => {
      mounted = true;
    }),
    unmount: vi.fn(() => {
      mounted = false;
    }),
    onClick: availableSync((_listener: VoidFunction) => {
      const off = vi.fn();
      removers.push(off);
      return off;
    }),
    show: availableSync(() => undefined),
    hide: availableSync(() => undefined),
    setText: availableSync((_text: string) => undefined),
    enable: availableSync(() => undefined),
    disable: availableSync(() => undefined),
    showLoader: availableSync(() => undefined),
    hideLoader: availableSync(() => undefined),
    setPosition: availableSync((_position: 'left' | 'right' | 'top' | 'bottom') => undefined),
  };
  return { provider, removers };
}

describe('Telegram SecondaryButton ownership', () => {
  it('replaces the current owner and makes stale disposal identity-safe', () => {
    const back = buttonProvider();
    const main = buttonProvider();
    const secondary = buttonProvider();
    const bridge = createTelegramButtonBridge({
      isBackButtonSupported: () => true,
      isSecondaryButtonSupported: () => true,
      backButton: back.provider,
      mainButton: main.provider,
      secondaryButton: secondary.provider,
    });

    const first = bridge.bindSecondary('Отмена', vi.fn(), 'left')!;
    const second = bridge.bindSecondary('Отмена', vi.fn(), 'left')!;

    expect(secondary.removers[0]).toHaveBeenCalledTimes(1);
    expect(secondary.provider.unmount).toHaveBeenCalledTimes(1);
    expect(secondary.provider.mount).toHaveBeenCalledTimes(2);

    first();
    expect(secondary.removers[1]).not.toHaveBeenCalled();
    expect(secondary.provider.unmount).toHaveBeenCalledTimes(1);

    second();
    expect(secondary.removers[1]).toHaveBeenCalledTimes(1);
    expect(secondary.provider.hide).toHaveBeenCalledTimes(2);
    expect(secondary.provider.unmount).toHaveBeenCalledTimes(2);
  });
});
