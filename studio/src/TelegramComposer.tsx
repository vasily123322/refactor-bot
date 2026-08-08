import Placeholder from '@tiptap/extension-placeholder';
import { EditorContent, useEditor } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { useEffect, useMemo } from 'react';

import {
  canEditDocument,
  documentToEditorJson,
  editorJsonToDocument,
  EditorAdapterError,
} from './editorAdapter';
import type { PostDocument } from './types';
import { documentText } from './types';

export function TelegramComposer({
  document,
  onChange,
}: {
  document: PostDocument;
  onChange: (document: PostDocument) => void;
}) {
  const editable = canEditDocument(document);
  const initialContent = useMemo(() => {
    try {
      return documentToEditorJson(document);
    } catch {
      return { type: 'doc', content: [{ type: 'paragraph' }] };
    }
  }, [document]);

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
        link: {
          openOnClick: false,
          autolink: true,
          defaultProtocol: 'https',
        },
      }),
      Placeholder.configure({
        placeholder: 'Напишите пост, вставьте материал или попросите AI создать черновик…',
      }),
    ],
    content: initialContent,
    editable,
    immediatelyRender: false,
    onUpdate({ editor: current }) {
      try {
        onChange(editorJsonToDocument(current.getJSON(), document));
      } catch (error) {
        if (!(error instanceof EditorAdapterError)) throw error;
      }
    },
  });

  useEffect(() => {
    if (!editor) return;
    editor.setEditable(editable);
    if (!editable) return;
    const next = documentToEditorJson(document);
    const current = JSON.stringify(editor.getJSON());
    if (current !== JSON.stringify(next)) {
      editor.commands.setContent(next, { emitUpdate: false });
    }
  }, [document, editable, editor]);

  if (!editable) {
    return (
      <div className="composer-readonly">
        <strong>Этот формат пока открыт только для просмотра.</strong>
        <p>{documentText(document) || 'В документе нет редактируемого текста.'}</p>
        <small>
          Исходный `PostDocument` сохранён без изменений. Renderer для этого формата будет
          подключён отдельно.
        </small>
      </div>
    );
  }

  if (!editor) return <div className="composer-loading">Загрузка редактора…</div>;

  const setLink = () => {
    const previous = editor.getAttributes('link').href as string | undefined;
    const next = window.prompt('Ссылка', previous || 'https://');
    if (next === null) return;
    const value = next.trim();
    if (!value) {
      editor.chain().focus().extendMarkRange('link').unsetLink().run();
      return;
    }
    editor.chain().focus().extendMarkRange('link').setLink({ href: value }).run();
  };

  const active = (name: string) => (editor.isActive(name) ? 'active' : '');

  return (
    <div className="tiptap-composer">
      <div className="format-toolbar" aria-label="Форматирование Telegram">
        <button
          className={active('bold')}
          title="Жирный"
          onClick={() => editor.chain().focus().toggleBold().run()}
        >
          <strong>B</strong>
        </button>
        <button
          className={active('italic')}
          title="Курсив"
          onClick={() => editor.chain().focus().toggleItalic().run()}
        >
          <em>I</em>
        </button>
        <button
          className={active('underline')}
          title="Подчёркнутый"
          onClick={() => editor.chain().focus().toggleUnderline().run()}
        >
          U
        </button>
        <button
          className={active('strike')}
          title="Зачёркнутый"
          onClick={() => editor.chain().focus().toggleStrike().run()}
        >
          S̶
        </button>
        <button
          className={active('code')}
          title="Моноширинный"
          onClick={() => editor.chain().focus().toggleCode().run()}
        >
          {'</>'}
        </button>
        <button className={active('link')} title="Ссылка" onClick={setLink}>
          🔗
        </button>
        <span className="toolbar-divider" />
        <button
          className={active('blockquote')}
          title="Цитата"
          onClick={() => editor.chain().focus().toggleBlockquote().run()}
        >
          ❞
        </button>
        <button
          className={active('bulletList')}
          title="Маркированный список"
          onClick={() => editor.chain().focus().toggleBulletList().run()}
        >
          •≡
        </button>
        <button
          className={active('orderedList')}
          title="Нумерованный список"
          onClick={() => editor.chain().focus().toggleOrderedList().run()}
        >
          1.
        </button>
        <span className="toolbar-spacer" />
        <button title="Отменить" onClick={() => editor.chain().focus().undo().run()}>
          ↶
        </button>
        <button title="Повторить" onClick={() => editor.chain().focus().redo().run()}>
          ↷
        </button>
        <span className="toolbar-mode">Classic · Telegram entities</span>
      </div>
      <EditorContent editor={editor} className="composer tiptap-editor" />
    </div>
  );
}
