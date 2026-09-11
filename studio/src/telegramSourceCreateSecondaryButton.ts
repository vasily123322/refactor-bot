import { useEffect } from 'react';

import { bindTelegramSecondaryButton } from './telegram';

export const SOURCE_CREATE_CANCEL_LABEL = 'Отмена';

export type TelegramSourceCreateSecondaryButtonState = Readonly<{
  active: boolean;
  onCancel: VoidFunction;
}>;

type SecondaryButtonBinder = typeof bindTelegramSecondaryButton;

export function bindTelegramSourceCreateSecondaryButton(
  state: TelegramSourceCreateSecondaryButtonState,
  bind: SecondaryButtonBinder = bindTelegramSecondaryButton,
): VoidFunction | null {
  if (!state.active) return null;
  return bind(SOURCE_CREATE_CANCEL_LABEL, state.onCancel, 'left');
}

export function useTelegramSourceCreateSecondaryButton(
  active: boolean,
  onCancel: VoidFunction,
): void {
  useEffect(() => {
    const dispose = bindTelegramSourceCreateSecondaryButton({ active, onCancel });
    return dispose ?? undefined;
  }, [active, onCancel]);
}
