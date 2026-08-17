import { useEffect } from 'react';

import { bindStudioTelegramBackNavigation } from './studioNavigation';
import type { StudioView } from './studioNavigation';
import { bindTelegramBackButton } from './telegram';

export function useTelegramStudioBackButton(
  view: StudioView,
  navigate: (target: StudioView) => void,
): void {
  useEffect(() => {
    const dispose = bindStudioTelegramBackNavigation(
      view,
      navigate,
      bindTelegramBackButton,
    );
    return dispose ?? undefined;
  }, [navigate, view]);
}
