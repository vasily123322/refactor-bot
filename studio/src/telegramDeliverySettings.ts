import type { PostDocument } from './types';

export type TelegramDeliverySettingsView = {
  silent: boolean;
  protectContent: boolean;
};

export function telegramDeliverySettings(document: PostDocument): TelegramDeliverySettingsView {
  const telegram = document.telegram;
  const silent = Object.prototype.hasOwnProperty.call(telegram, 'silent')
    ? Boolean(telegram.silent)
    : Boolean(telegram.disable_notification);
  return {
    silent,
    protectContent: Boolean(telegram.protect_content),
  };
}

export function patchTelegramDeliverySettings(
  document: PostDocument,
  patch: Partial<TelegramDeliverySettingsView>,
): PostDocument {
  const telegram = { ...document.telegram };

  if (patch.silent !== undefined) {
    delete telegram.disable_notification;
    if (patch.silent) telegram.silent = true;
    else delete telegram.silent;
  }

  if (patch.protectContent !== undefined) {
    if (patch.protectContent) telegram.protect_content = true;
    else delete telegram.protect_content;
  }

  return { ...document, telegram };
}
