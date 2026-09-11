export type TelegramSecondaryButtonPosition = 'left' | 'right' | 'top' | 'bottom';
export type TelegramMainButtonPresentation = Readonly<{
  enabled?: boolean;
  loading?: boolean;
}>;

type AvailableSync<Args extends unknown[], Value> = ((...args: Args) => Value) & {
  isAvailable(): boolean;
};

type ButtonBase = Readonly<{
  isMounted(): boolean;
  mount: AvailableSync<[], void>;
  unmount(): void;
  onClick: AvailableSync<[listener: VoidFunction], VoidFunction>;
  show: AvailableSync<[], void>;
  hide: AvailableSync<[], void>;
}>;

type BackButtonProvider = ButtonBase & Readonly<{
  isSupported(): boolean;
}>;

type MainButtonProvider = ButtonBase & Readonly<{
  setText: AvailableSync<[text: string], void>;
  enable: AvailableSync<[], void>;
  disable: AvailableSync<[], void>;
  showLoader: AvailableSync<[], void>;
  hideLoader: AvailableSync<[], void>;
}>;

type SecondaryButtonProvider = ButtonBase & Readonly<{
  isSupported(): boolean;
  setText: AvailableSync<[text: string], void>;
  setPosition: AvailableSync<[position: TelegramSecondaryButtonPosition], void>;
}>;

export type TelegramButtonBridgeDependencies = Readonly<{
  isBackButtonSupported(): boolean;
  isSecondaryButtonSupported(): boolean;
  backButton: BackButtonProvider;
  mainButton: MainButtonProvider;
  secondaryButton: SecondaryButtonProvider;
}>;

type BindingSlot = { release?: VoidFunction };

function replaceBinding(
  slot: BindingSlot,
  setup: () => VoidFunction | null,
): VoidFunction | null {
  slot.release?.();
  slot.release = undefined;

  const release = setup();
  if (!release) return null;

  let active = true;
  const ownedRelease = () => {
    if (!active) return;
    active = false;
    release();
    if (slot.release === ownedRelease) slot.release = undefined;
  };
  slot.release = ownedRelease;
  return ownedRelease;
}

function mountButton(button: ButtonBase): boolean {
  if (!button.mount.isAvailable()) return false;
  if (!button.isMounted()) button.mount();
  return button.isMounted();
}

function cleanupButton(button: ButtonBase, off: VoidFunction): void {
  off();
  if (button.isMounted() && button.hide.isAvailable()) button.hide();
  if (button.isMounted()) button.unmount();
}

export function createTelegramButtonBridge(deps: TelegramButtonBridgeDependencies) {
  const backSlot: BindingSlot = {};
  const mainSlot: BindingSlot = {};
  const secondarySlot: BindingSlot = {};

  function bindBack(listener: VoidFunction): VoidFunction | null {
    return replaceBinding(backSlot, () => {
      const button = deps.backButton;
      if (
        !deps.isBackButtonSupported() ||
        !button.isSupported() ||
        !mountButton(button) ||
        !button.onClick.isAvailable() ||
        !button.show.isAvailable()
      ) {
        if (button.isMounted()) button.unmount();
        return null;
      }
      const off = button.onClick(listener);
      button.show();
      return () => cleanupButton(button, off);
    });
  }

  function bindMain(
    text: string,
    listener: VoidFunction,
    presentation: TelegramMainButtonPresentation = {},
  ): VoidFunction | null {
    const normalizedText = text.trim();
    if (!normalizedText) return null;

    return replaceBinding(mainSlot, () => {
      const button = deps.mainButton;
      const enabledAction = presentation.enabled === false ? button.disable : button.enable;
      const loadingAction = presentation.loading ? button.showLoader : button.hideLoader;
      if (
        !mountButton(button) ||
        !button.setText.isAvailable() ||
        !enabledAction.isAvailable() ||
        !loadingAction.isAvailable() ||
        !button.onClick.isAvailable() ||
        !button.show.isAvailable()
      ) {
        if (button.isMounted()) button.unmount();
        return null;
      }
      button.setText(normalizedText);
      enabledAction();
      loadingAction();
      const off = button.onClick(listener);
      button.show();
      return () => cleanupButton(button, off);
    });
  }

  function bindSecondary(
    text: string,
    listener: VoidFunction,
    position: TelegramSecondaryButtonPosition = 'left',
  ): VoidFunction | null {
    const normalizedText = text.trim();
    if (!normalizedText) return null;

    return replaceBinding(secondarySlot, () => {
      const button = deps.secondaryButton;
      if (
        !deps.isSecondaryButtonSupported() ||
        !button.isSupported() ||
        !mountButton(button) ||
        !button.setText.isAvailable() ||
        !button.setPosition.isAvailable() ||
        !button.onClick.isAvailable() ||
        !button.show.isAvailable()
      ) {
        if (button.isMounted()) button.unmount();
        return null;
      }
      button.setText(normalizedText);
      button.setPosition(position);
      const off = button.onClick(listener);
      button.show();
      return () => cleanupButton(button, off);
    });
  }

  function disposeAll(): void {
    backSlot.release?.();
    mainSlot.release?.();
    secondarySlot.release?.();
  }

  return Object.freeze({ bindBack, bindMain, bindSecondary, disposeAll });
}
