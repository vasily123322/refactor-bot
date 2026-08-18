import { describe, expect, it, vi } from 'vitest';

import { createTelegramButtonBridge } from './telegramButtons';

function availableSync<Args extends unknown[], Value>(
  implementation: (...args: Args) => Value,
  available = true,
) {
  return Object.assign(vi.fn(implementation), {
    isAvailable: vi.fn(() => available),
  });
}

function buttonProvider(options: { supported?: boolean } = {}) {
  let mounted = false;
  const listeners: VoidFunction[] = [];
  const removers: ReturnType<typeof vi.fn>[] = [];

  const provider = {
    isSupported: vi.fn(() => options.supported ?? true),
    isMounted: vi.fn(() => mounted),
    mount: availableSync(() => {
      mounted = true;
    }),
    unmount: vi.fn(() => {
      mounted = false;
    }),
    onClick: availableSync((listener: VoidFunction) => {
      listeners.push(listener);
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

  return { provider, listeners, removers };
}

function bridgeWithProviders() {
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
  return { bridge, back, main, secondary };
}

describe('Telegram native button bridge', () => {
  it('fails closed without mounting unsupported BackButton clients', () => {
    const back = buttonProvider({ supported: false });
    const main = buttonProvider();
    const secondary = buttonProvider();
    const bridge = createTelegramButtonBridge({
      isBackButtonSupported: () => true,
      isSecondaryButtonSupported: () => true,
      backButton: back.provider,
      mainButton: main.provider,
      secondaryButton: secondary.provider,
    });

    expect(bridge.bindBack(vi.fn())).toBeNull();
    expect(back.provider.mount).not.toHaveBeenCalled();
    expect(back.provider.onClick).not.toHaveBeenCalled();
  });

  it('owns the full main-button mount → listener → hide → unmount lifecycle', () => {
    const { bridge, main } = bridgeWithProviders();
    const listener = vi.fn();

    const release = bridge.bindMain('  Publish  ', listener);
    expect(release).not.toBeNull();
    expect(main.provider.mount).toHaveBeenCalledTimes(1);
    expect(main.provider.setText).toHaveBeenCalledWith('Publish');
    expect(main.provider.enable).toHaveBeenCalledTimes(1);
    expect(main.provider.hideLoader).toHaveBeenCalledTimes(1);
    expect(main.provider.onClick).toHaveBeenCalledWith(listener);
    expect(main.provider.show).toHaveBeenCalledTimes(1);

    release!();
    expect(main.removers[0]).toHaveBeenCalledTimes(1);
    expect(main.provider.hide).toHaveBeenCalledTimes(1);
    expect(main.provider.unmount).toHaveBeenCalledTimes(1);

    release!();
    expect(main.removers[0]).toHaveBeenCalledTimes(1);
    expect(main.provider.unmount).toHaveBeenCalledTimes(1);
  });

  it('projects disabled and loading state through the owned MainButton binding', () => {
    const { bridge, main } = bridgeWithProviders();

    expect(bridge.bindMain('Preview', vi.fn(), {
      enabled: false,
      loading: true,
    })).not.toBeNull();

    expect(main.provider.disable).toHaveBeenCalledTimes(1);
    expect(main.provider.enable).not.toHaveBeenCalled();
    expect(main.provider.showLoader).toHaveBeenCalledTimes(1);
    expect(main.provider.hideLoader).not.toHaveBeenCalled();
  });

  it('replaces a React remount owner without leaking the previous listener', () => {
    const { bridge, main } = bridgeWithProviders();
    const first = vi.fn();
    const second = vi.fn();

    const releaseFirst = bridge.bindMain('Save', first)!;
    const releaseSecond = bridge.bindMain('Save', second, {
      enabled: false,
      loading: true,
    })!;

    expect(main.removers[0]).toHaveBeenCalledTimes(1);
    expect(main.provider.unmount).toHaveBeenCalledTimes(1);
    expect(main.provider.mount).toHaveBeenCalledTimes(2);
    expect(main.provider.onClick).toHaveBeenCalledTimes(2);
    expect(main.provider.disable).toHaveBeenCalledTimes(1);
    expect(main.provider.showLoader).toHaveBeenCalledTimes(1);

    releaseFirst();
    expect(main.removers[1]).not.toHaveBeenCalled();
    expect(main.provider.unmount).toHaveBeenCalledTimes(1);

    releaseSecond();
    expect(main.removers[1]).toHaveBeenCalledTimes(1);
    expect(main.provider.unmount).toHaveBeenCalledTimes(2);
  });

  it('binds SecondaryButton position and releases its ownership symmetrically', () => {
    const { bridge, secondary } = bridgeWithProviders();
    const release = bridge.bindSecondary('Cancel', vi.fn(), 'right');

    expect(release).not.toBeNull();
    expect(secondary.provider.setText).toHaveBeenCalledWith('Cancel');
    expect(secondary.provider.setPosition).toHaveBeenCalledWith('right');
    expect(secondary.provider.show).toHaveBeenCalledTimes(1);

    release!();
    expect(secondary.removers[0]).toHaveBeenCalledTimes(1);
    expect(secondary.provider.hide).toHaveBeenCalledTimes(1);
    expect(secondary.provider.unmount).toHaveBeenCalledTimes(1);
  });

  it('disposeAll releases every active native button owner', () => {
    const { bridge, back, main, secondary } = bridgeWithProviders();
    expect(bridge.bindBack(vi.fn())).not.toBeNull();
    expect(bridge.bindMain('Continue', vi.fn())).not.toBeNull();
    expect(bridge.bindSecondary('Cancel', vi.fn())).not.toBeNull();

    bridge.disposeAll();

    expect(back.removers[0]).toHaveBeenCalledTimes(1);
    expect(main.removers[0]).toHaveBeenCalledTimes(1);
    expect(secondary.removers[0]).toHaveBeenCalledTimes(1);
    expect(back.provider.unmount).toHaveBeenCalledTimes(1);
    expect(main.provider.unmount).toHaveBeenCalledTimes(1);
    expect(secondary.provider.unmount).toHaveBeenCalledTimes(1);
  });

  it('does not mount any button during bridge construction', () => {
    const { back, main, secondary } = bridgeWithProviders();
    expect(back.provider.mount).not.toHaveBeenCalled();
    expect(main.provider.mount).not.toHaveBeenCalled();
    expect(secondary.provider.mount).not.toHaveBeenCalled();
  });
});
