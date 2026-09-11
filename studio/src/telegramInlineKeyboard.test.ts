import { describe, expect, it } from 'vitest';

import { emptyRichDocument } from './types';
import {
  inlineButtonFromDraft,
  moveKeyboardButton,
  moveKeyboardRow,
  patchTelegramInlineKeyboard,
  telegramInlineKeyboard,
  validateInlineButtonDraft,
} from './telegramInlineKeyboard';

describe('Telegram inline keyboard authoring', () => {
  it('mirrors renderer URL priority when both URL and callback_data exist', () => {
    const document = emptyRichDocument();
    document.telegram.buttons = [[{
      text: 'Open',
      url: 'https://example.com',
      callback_data: 'ignored-by-renderer',
    }]];
    expect(telegramInlineKeyboard(document)).toEqual({
      kind: 'editable',
      rows: [[{ text: 'Open', url: 'https://example.com' }]],
    });
  });

  it('validates URL schemes and callback_data UTF-8 byte limit before commit', () => {
    expect(validateInlineButtonDraft({ text: 'Web', mode: 'url', value: 'https://example.com' })).toBeNull();
    expect(validateInlineButtonDraft({ text: 'TG', mode: 'url', value: 'tg://resolve?domain=test' })).toBeNull();
    expect(validateInlineButtonDraft({ text: 'Bad', mode: 'url', value: 'javascript:alert(1)' })).toMatch(/URL/);

    expect(validateInlineButtonDraft({ text: 'Action', mode: 'callback', value: 'a'.repeat(64) })).toBeNull();
    expect(validateInlineButtonDraft({ text: 'Action', mode: 'callback', value: 'я'.repeat(32) })).toBeNull();
    expect(validateInlineButtonDraft({ text: 'Action', mode: 'callback', value: 'я'.repeat(33) })).toMatch(/64 bytes/);
    expect(validateInlineButtonDraft({ text: '', mode: 'callback', value: 'ok' })).toMatch(/текст/);
  });

  it('creates exactly one action field from an atomic draft', () => {
    expect(inlineButtonFromDraft({ text: ' Open ', mode: 'url', value: ' https://example.com ' })).toEqual({
      text: 'Open',
      url: 'https://example.com',
    });
    expect(inlineButtonFromDraft({ text: 'Do', mode: 'callback', value: ' action ' })).toEqual({
      text: 'Do',
      callback_data: 'action',
    });
  });

  it('fails closed on malformed stored keyboard instead of silently dropping it', () => {
    const document = emptyRichDocument();
    document.telegram = { buttons: [[{ text: '', callback_data: 'x' }]] };
    expect(telegramInlineKeyboard(document).kind).toBe('invalid');
  });

  it('patches only buttons and preserves other Telegram settings', () => {
    const document = emptyRichDocument();
    document.telegram = { silent: true, protect_content: true, future: 'keep' };
    const next = patchTelegramInlineKeyboard(document, [[{ text: 'Open', url: 'https://example.com' }]]);
    expect(next?.telegram).toEqual({
      silent: true,
      protect_content: true,
      future: 'keep',
      buttons: [[{ text: 'Open', url: 'https://example.com' }]],
    });
    expect(patchTelegramInlineKeyboard(next!, [])?.telegram).toEqual({
      silent: true,
      protect_content: true,
      future: 'keep',
    });
  });

  it('moves rows and buttons without mutating the source', () => {
    const rows = [
      [{ text: 'A', callback_data: 'a' }, { text: 'B', callback_data: 'b' }],
      [{ text: 'C', callback_data: 'c' }],
    ];
    expect(moveKeyboardButton(rows, 0, 0, 1)[0].map((button) => button.text)).toEqual(['B', 'A']);
    expect(moveKeyboardRow(rows, 0, 1)[0][0].text).toBe('C');
    expect(rows[0].map((button) => button.text)).toEqual(['A', 'B']);
  });
});

describe('renderer-mirrored stored action precedence', () => {
  it('fails closed when stored url is whitespace-only even if callback_data exists', () => {
    const document = emptyRichDocument();
    document.telegram = { buttons: [[{ text: 'X', url: ' ', callback_data: 'x' }]] };
    const state = telegramInlineKeyboard(document);
    expect(state.kind).toBe('invalid');
    expect(state.kind === 'invalid' && state.reason).toMatch(/URL/);
  });

  it('treats any other truthy stored url value as url and fails closed', () => {
    const document = emptyRichDocument();
    document.telegram = { buttons: [[{ text: 'X', url: 42 as unknown as string }]] };
    expect(telegramInlineKeyboard(document).kind).toBe('invalid');
  });
});
