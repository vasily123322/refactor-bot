import { describe, expect, it } from 'vitest';

import { emptyRichDocument } from './types';
import {
  patchTelegramDeliverySettings,
  telegramDeliverySettings,
} from './telegramDeliverySettings';

describe('Telegram delivery settings', () => {
  it('mirrors renderer alias precedence for silent delivery', () => {
    const document = emptyRichDocument();
    document.telegram = { disable_notification: true };
    expect(telegramDeliverySettings(document).silent).toBe(true);

    document.telegram = { silent: false, disable_notification: true };
    expect(telegramDeliverySettings(document).silent).toBe(false);
  });

  it('writes one canonical silent key and removes the legacy alias', () => {
    const document = emptyRichDocument();
    document.telegram = { disable_notification: false, buttons: [[{ text: 'Open', url: 'https://example.com' }]] };

    const enabled = patchTelegramDeliverySettings(document, { silent: true });
    expect(enabled.telegram).toEqual({
      silent: true,
      buttons: [[{ text: 'Open', url: 'https://example.com' }]],
    });

    const disabled = patchTelegramDeliverySettings(enabled, { silent: false });
    expect(disabled.telegram).toEqual({
      buttons: [[{ text: 'Open', url: 'https://example.com' }]],
    });
  });

  it('toggles protect_content without disturbing unrelated Telegram settings', () => {
    const document = emptyRichDocument();
    document.telegram = { custom_future_setting: 'keep-me' };

    const enabled = patchTelegramDeliverySettings(document, { protectContent: true });
    expect(enabled.telegram).toEqual({
      custom_future_setting: 'keep-me',
      protect_content: true,
    });
    expect(telegramDeliverySettings(enabled)).toEqual({ silent: false, protectContent: true });

    const disabled = patchTelegramDeliverySettings(enabled, { protectContent: false });
    expect(disabled.telegram).toEqual({ custom_future_setting: 'keep-me' });
  });

  it('does not mutate the source document', () => {
    const document = emptyRichDocument();
    const next = patchTelegramDeliverySettings(document, { silent: true, protectContent: true });
    expect(document.telegram).toEqual({});
    expect(next.telegram).toEqual({ silent: true, protect_content: true });
  });
});
