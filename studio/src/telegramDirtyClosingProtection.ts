import { useEffect, useRef } from 'react';

import { createDirtyClosingProtectionController } from './dirtyClosingProtection';
import {
  disposeTelegramClosingConfirmation,
  setTelegramClosingConfirmation,
} from './telegram';

const defaultBridge = {
  enable: () => setTelegramClosingConfirmation(true),
  dispose: () => disposeTelegramClosingConfirmation(),
};

export function useTelegramDirtyClosingProtection(dirty: boolean): void {
  const controllerRef = useRef<
    ReturnType<typeof createDirtyClosingProtectionController> | null
  >(null);

  if (controllerRef.current === null) {
    controllerRef.current = createDirtyClosingProtectionController(defaultBridge);
  }

  useEffect(() => {
    controllerRef.current?.sync(dirty);
  }, [dirty]);

  useEffect(
    () => () => {
      controllerRef.current?.dispose();
    },
    [],
  );
}
