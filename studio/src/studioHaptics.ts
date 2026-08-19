import { studioHapticNotificationType } from './studioHapticsPolicy';
import type { StudioHapticEvent } from './studioHapticsPolicy';
import { triggerTelegramHapticNotification } from './telegram';

export function emitStudioHaptic(event: StudioHapticEvent): boolean {
  return triggerTelegramHapticNotification(studioHapticNotificationType(event));
}
