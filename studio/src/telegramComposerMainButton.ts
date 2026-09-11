import { useEffect } from 'react';

import { bindTelegramMainButton } from './telegram';

export type TelegramComposerMainButtonState = Readonly<{
  active: boolean;
  label: string;
  disabled: boolean;
  loading: boolean;
  onSubmit: VoidFunction;
}>;

type MainButtonBinder = typeof bindTelegramMainButton;

export function bindTelegramComposerMainButton(
  state: TelegramComposerMainButtonState,
  bind: MainButtonBinder = bindTelegramMainButton,
): VoidFunction | null {
  if (!state.active) return null;
  return bind(state.label, state.onSubmit, {
    enabled: !state.disabled,
    loading: state.loading,
  });
}

export function useTelegramComposerMainButton(
  state: TelegramComposerMainButtonState,
): void {
  useEffect(() => {
    const dispose = bindTelegramComposerMainButton(state);
    return dispose ?? undefined;
  }, [state.active, state.disabled, state.label, state.loading, state.onSubmit]);
}
