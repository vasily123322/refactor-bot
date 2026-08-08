import type { JSONContent } from '@tiptap/core';
import { describe, expect, it } from 'vitest';

import {
  editorJsonToRichContent,
  richContentPlainText,
  richContentToEditorJson,
} from './richTextAdapter';

describe('RichText editor adapter', () => {
  it('round-trips supported native rich marks', () => {
    const content = [
      { text: 'Bold', marks: ['bold'] },
      { text: ' + ' },
      { text: 'link', marks: [{ type: 'link', url: 'https://example.com' }] },
    ];
    const json = richContentToEditorJson(content);
    expect(editorJsonToRichContent(json)).toEqual(content);
    expect(richContentPlainText(content)).toBe('Bold + link');
  });

  it('normalizes Tiptap strike to Telegram strikethrough mark', () => {
    const json: JSONContent = {
      type: 'doc',
      content: [
        {
          type: 'paragraph',
          content: [{ type: 'text', text: 'old', marks: [{ type: 'strike' }] }],
        },
      ],
    };
    expect(editorJsonToRichContent(json)).toEqual([
      { text: 'old', marks: ['strikethrough'] },
    ]);
  });

  it('ignores unsupported editor marks instead of persisting UI-specific state', () => {
    const json: JSONContent = {
      type: 'doc',
      content: [
        {
          type: 'paragraph',
          content: [
            { type: 'text', text: 'safe', marks: [{ type: 'future-ui-mark' }] },
          ],
        },
      ],
    };
    expect(editorJsonToRichContent(json)).toEqual([{ text: 'safe' }]);
  });
});
