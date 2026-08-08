import type { JSONContent } from '@tiptap/core';
import { describe, expect, it } from 'vitest';

import {
  canEditDocument,
  documentToEditorJson,
  editorJsonToDocument,
  EditorAdapterError,
} from './editorAdapter';
import type { PostDocument, TelegramEntity } from './types';

function baseDocument(text = ''): PostDocument {
  return {
    schema_version: 1,
    mode: 'classic',
    blocks: [{ id: 'b1', type: 'text', text, entities: [] }],
    telegram: {},
    metadata: {},
  };
}

function entities(document: PostDocument): TelegramEntity[] {
  return document.blocks[0].entities ?? [];
}

describe('Tiptap → Telegram entity adapter', () => {
  it('compiles formatting and links into Telegram ranges', () => {
    const json: JSONContent = {
      type: 'doc',
      content: [
        {
          type: 'paragraph',
          content: [
            { type: 'text', text: 'Hello', marks: [{ type: 'bold' }] },
            { type: 'text', text: ' ' },
            {
              type: 'text',
              text: 'world',
              marks: [
                { type: 'italic' },
                { type: 'link', attrs: { href: 'https://example.com' } },
              ],
            },
          ],
        },
      ],
    };

    const document = editorJsonToDocument(json, baseDocument());
    expect(document.blocks[0].text).toBe('Hello world');
    expect(entities(document)).toEqual([
      { type: 'bold', offset: 0, length: 5 },
      { type: 'italic', offset: 6, length: 5 },
      { type: 'text_link', offset: 6, length: 5, url: 'https://example.com' },
    ]);
  });

  it('uses Telegram UTF-16 offsets for emoji', () => {
    const json: JSONContent = {
      type: 'doc',
      content: [
        {
          type: 'paragraph',
          content: [
            { type: 'text', text: '🔥' },
            { type: 'text', text: 'Hello', marks: [{ type: 'bold' }] },
          ],
        },
      ],
    };

    const document = editorJsonToDocument(json, baseDocument());
    expect(document.blocks[0].text).toBe('🔥Hello');
    expect('🔥'.length).toBe(2);
    expect(entities(document)).toEqual([{ type: 'bold', offset: 2, length: 5 }]);
  });

  it('keeps overlapping nested marks', () => {
    const json: JSONContent = {
      type: 'doc',
      content: [
        {
          type: 'paragraph',
          content: [
            {
              type: 'text',
              text: 'nested',
              marks: [{ type: 'bold' }, { type: 'italic' }, { type: 'underline' }],
            },
          ],
        },
      ],
    };

    const document = editorJsonToDocument(json, baseDocument());
    expect(entities(document)).toEqual([
      { type: 'bold', offset: 0, length: 6 },
      { type: 'italic', offset: 0, length: 6 },
      { type: 'underline', offset: 0, length: 6 },
    ]);
  });

  it('renders lists and blockquotes into Telegram-compatible text', () => {
    const json: JSONContent = {
      type: 'doc',
      content: [
        {
          type: 'bulletList',
          content: [
            { type: 'listItem', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'One' }] }] },
            { type: 'listItem', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'Two' }] }] },
          ],
        },
        {
          type: 'blockquote',
          content: [{ type: 'paragraph', content: [{ type: 'text', text: 'Quote' }] }],
        },
      ],
    };

    const document = editorJsonToDocument(json, baseDocument());
    expect(document.blocks[0].text).toBe('• One\n• Two\nQuote');
    expect(entities(document)).toContainEqual({ type: 'blockquote', offset: 12, length: 5 });
  });
});

describe('Telegram entities → Tiptap adapter', () => {
  it('round-trips text, emoji and inline formatting semantically', () => {
    const original: PostDocument = {
      ...baseDocument('🔥Hello link'),
      blocks: [
        {
          id: 'b1',
          type: 'text',
          text: '🔥Hello link',
          entities: [
            { type: 'bold', offset: 2, length: 5 },
            { type: 'text_link', offset: 8, length: 4, url: 'https://example.com' },
          ],
        },
      ],
    };

    const json = documentToEditorJson(original);
    const rebuilt = editorJsonToDocument(json, original);

    expect(rebuilt.blocks[0].text).toBe(original.blocks[0].text);
    expect(rebuilt.blocks[0].entities).toEqual(original.blocks[0].entities);
  });

  it('ignores unknown entity types when loading instead of corrupting text', () => {
    const document = baseDocument('hello');
    document.blocks[0].entities = [
      { type: 'future_entity', offset: 0, length: 5 } as unknown as TelegramEntity,
    ];
    const json = documentToEditorJson(document);
    const rebuilt = editorJsonToDocument(json, baseDocument());
    expect(rebuilt.blocks[0].text).toBe('hello');
    expect(rebuilt.blocks[0].entities).toEqual([]);
  });

  it('fails explicitly for non-editable formats', () => {
    const document: PostDocument = {
      schema_version: 1,
      mode: 'classic',
      blocks: [{ id: 'p1', type: 'poll', question: 'Question?' }],
      telegram: {},
      metadata: {},
    };
    expect(canEditDocument(document)).toBe(false);
    expect(() => documentToEditorJson(document)).toThrow(EditorAdapterError);
  });
});
