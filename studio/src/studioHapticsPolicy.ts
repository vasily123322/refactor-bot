export type StudioHapticEvent =
  | 'manual-save-success'
  | 'manual-save-error'
  | 'publish-success'
  | 'publish-error'
  | 'destructive-confirmation'
  | 'validation-error'
  | 'action-error';

export type StudioHapticNotificationType = 'success' | 'warning' | 'error';

export function studioHapticNotificationType(
  event: StudioHapticEvent,
): StudioHapticNotificationType {
  switch (event) {
    case 'manual-save-success':
    case 'publish-success':
      return 'success';
    case 'destructive-confirmation':
      return 'warning';
    case 'manual-save-error':
    case 'publish-error':
    case 'validation-error':
    case 'action-error':
      return 'error';
  }
}
