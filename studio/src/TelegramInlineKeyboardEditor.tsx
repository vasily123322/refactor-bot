import './telegram-inline-keyboard.css';

import { useState } from 'react';

import {
  inlineButtonDraft,
  inlineButtonFromDraft,
  moveKeyboardButton,
  moveKeyboardRow,
  patchTelegramInlineKeyboard,
  telegramInlineKeyboard,
  validateInlineButtonDraft,
  type InlineButtonDraft,
  type InlineButtonMode,
} from './telegramInlineKeyboard';
import type { InlineButton, PostDocument } from './types';

function promptButton(
  mode: InlineButtonMode,
  current?: InlineButton,
): { button: InlineButton | null; error: string | null } {
  const previous = current ? inlineButtonDraft(current) : null;
  const text = window.prompt('Текст кнопки', previous?.text ?? 'Кнопка');
  if (text === null) return { button: null, error: null };
  const value = window.prompt(
    mode === 'url' ? 'HTTP / HTTPS / tg:// URL' : 'callback_data (1–64 bytes)',
    previous?.mode === mode ? previous.value : mode === 'url' ? 'https://' : 'action',
  );
  if (value === null) return { button: null, error: null };
  const draft: InlineButtonDraft = { text, mode, value };
  const error = validateInlineButtonDraft(draft);
  return { button: error ? null : inlineButtonFromDraft(draft), error };
}

function withoutButton(rows: InlineButton[][], rowIndex: number, buttonIndex: number): InlineButton[][] {
  return rows
    .map((row, currentRow) =>
      currentRow === rowIndex
        ? row.filter((_, currentButton) => currentButton !== buttonIndex)
        : [...row],
    )
    .filter((row) => row.length > 0);
}

export function TelegramInlineKeyboardEditor({
  document,
  onChange,
}: {
  document: PostDocument;
  onChange: (document: PostDocument) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const keyboard = telegramInlineKeyboard(document);

  const commit = (rows: InlineButton[][]) => {
    const next = patchTelegramInlineKeyboard(document, rows);
    if (!next) {
      setError('Keyboard содержит некорректную кнопку');
      return;
    }
    setError(null);
    onChange(next);
  };

  const addRow = (mode: InlineButtonMode) => {
    const result = promptButton(mode);
    if (result.error) {
      setError(result.error);
      return;
    }
    if (!result.button || keyboard.kind !== 'editable') return;
    commit([...keyboard.rows, [result.button]]);
  };

  const addButton = (rowIndex: number, mode: InlineButtonMode) => {
    const result = promptButton(mode);
    if (result.error) {
      setError(result.error);
      return;
    }
    if (!result.button || keyboard.kind !== 'editable') return;
    commit(keyboard.rows.map((row, current) =>
      current === rowIndex ? [...row, result.button!] : [...row],
    ));
  };

  const editButton = (rowIndex: number, buttonIndex: number, button: InlineButton) => {
    const mode = inlineButtonDraft(button).mode;
    const result = promptButton(mode, button);
    if (result.error) {
      setError(result.error);
      return;
    }
    if (!result.button || keyboard.kind !== 'editable') return;
    commit(keyboard.rows.map((row, currentRow) =>
      currentRow === rowIndex
        ? row.map((candidate, currentButton) => currentButton === buttonIndex ? result.button! : candidate)
        : [...row],
    ));
  };

  if (keyboard.kind === 'invalid') {
    return (
      <section className="telegram-inline-keyboard" aria-label="Inline keyboard">
        <div className="telegram-inline-keyboard-head">
          <div>
            <strong>Inline keyboard</strong>
            <small>Stored keyboard не соответствует production renderer contract.</small>
          </div>
          <button onClick={() => commit([])}>Сбросить keyboard</button>
        </div>
        <div className="rich-media-warning">{keyboard.reason}</div>
      </section>
    );
  }

  return (
    <section className="telegram-inline-keyboard" aria-label="Inline keyboard">
      <div className="telegram-inline-keyboard-head">
        <div>
          <strong>Inline keyboard</strong>
          <small>URL или callback_data. Изменения применяются только после полной валидации.</small>
        </div>
        <div>
          <button onClick={() => addRow('url')}>+ URL row</button>
          <button onClick={() => addRow('callback')}>+ Callback row</button>
        </div>
      </div>

      {error && <div className="rich-media-warning">{error}</div>}
      {keyboard.rows.length === 0 && (
        <small className="telegram-inline-keyboard-empty">Кнопки не добавлены.</small>
      )}

      <div className="telegram-inline-keyboard-rows">
        {keyboard.rows.map((row, rowIndex) => (
          <div className="telegram-inline-keyboard-row" key={`keyboard-row:${rowIndex}`}>
            <div className="telegram-inline-keyboard-row-actions">
              <strong>Ряд {rowIndex + 1}</strong>
              <button
                disabled={rowIndex === 0}
                onClick={() => commit(moveKeyboardRow(keyboard.rows, rowIndex, -1))}
                title="Ряд выше"
              >↑</button>
              <button
                disabled={rowIndex === keyboard.rows.length - 1}
                onClick={() => commit(moveKeyboardRow(keyboard.rows, rowIndex, 1))}
                title="Ряд ниже"
              >↓</button>
              <button onClick={() => addButton(rowIndex, 'url')}>+ URL</button>
              <button onClick={() => addButton(rowIndex, 'callback')}>+ Callback</button>
            </div>
            <div className="telegram-inline-keyboard-buttons">
              {row.map((button, buttonIndex) => {
                const draft = inlineButtonDraft(button);
                return (
                  <div className="telegram-inline-keyboard-button" key={`${rowIndex}:${buttonIndex}:${button.text}`}>
                    <button className="telegram-inline-keyboard-label" onClick={() => editButton(rowIndex, buttonIndex, button)}>
                      <strong>{button.text}</strong>
                      <small>{draft.mode === 'url' ? 'URL' : 'callback'} · {draft.value}</small>
                    </button>
                    <button
                      disabled={buttonIndex === 0}
                      onClick={() => commit(moveKeyboardButton(keyboard.rows, rowIndex, buttonIndex, -1))}
                      title="Левее"
                    >←</button>
                    <button
                      disabled={buttonIndex === row.length - 1}
                      onClick={() => commit(moveKeyboardButton(keyboard.rows, rowIndex, buttonIndex, 1))}
                      title="Правее"
                    >→</button>
                    <button
                      onClick={() => commit(withoutButton(keyboard.rows, rowIndex, buttonIndex))}
                      title="Удалить"
                    >×</button>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
