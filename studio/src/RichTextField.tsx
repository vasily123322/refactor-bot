import Placeholder from '@tiptap/extension-placeholder';
import { EditorContent, useEditor } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { useEffect, useMemo } from 'react';

import {
  editorJsonToRichContent,
  type RichContent,
  richContentToEditorJson,
} from './richTextAdapter';

export function RichTextField({
  value,
  onChange,
  placeholder,
  compact = false,
}: {
  value: RichContent | undefined;
  onChange: (value: RichContent) => void;
  placeholder: string;
  compact?: boolean;
}) {
  const initial = useMemo(() => richContentToEditorJson(value), [value]);
  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: false,
        blockquote: false,
        bulletList: false,
        orderedList: false,
        listItem: false,
        codeBlock: false,
        horizontalRule: false,
        link: { openOnClick: false, autolink: true, defaultProtocol: 'https' },
      }),
      Placeholder.configure({ placeholder }),
    ],
    content: initial,
    immediatelyRender: false,
    onUpdate({ editor: current }) {
      onChange(editorJsonToRichContent(current.getJSON()));
    },
  });

  useEffect(() => {
    if (!editor) return;
    const next = richContentToEditorJson(value);
    if (JSON.stringify(editor.getJSON()) !== JSON.stringify(next)) {
      editor.commands.setContent(next, { emitUpdate: false });
    }
  }, [editor, value]);

  if (!editor) return <div className="rich-inline-loading">…</div>;

  const setLink = () => {
    const previous = editor.getAttributes('link').href as string | undefined;
    const next = window.prompt('Ссылка', previous || 'https://');
    if (next === null) return;
    const href = next.trim();
    if (!href) {
      editor.chain().focus().extendMarkRange('link').unsetLink().run();
      return;
    }
    editor.chain().focus().extendMarkRange('link').setLink({ href }).run();
  };

  return (
    <div className={compact ? 'rich-inline-field compact' : 'rich-inline-field'}>
      <div className="rich-inline-toolbar">
        <button className={editor.isActive('bold') ? 'active' : ''} onClick={() => editor.chain().focus().toggleBold().run()} title="Жирный"><strong>B</strong></button>
        <button className={editor.isActive('italic') ? 'active' : ''} onClick={() => editor.chain().focus().toggleItalic().run()} title="Курсив"><em>I</em></button>
        <button className={editor.isActive('underline') ? 'active' : ''} onClick={() => editor.chain().focus().toggleUnderline().run()} title="Подчёркнутый">U</button>
        <button className={editor.isActive('strike') ? 'active' : ''} onClick={() => editor.chain().focus().toggleStrike().run()} title="Зачёркнутый">S̶</button>
        <button className={editor.isActive('code') ? 'active' : ''} onClick={() => editor.chain().focus().toggleCode().run()} title="Code">{'</>'}</button>
        <button className={editor.isActive('link') ? 'active' : ''} onClick={setLink} title="Ссылка">🔗</button>
      </div>
      <EditorContent editor={editor} className="rich-inline-editor" />
    </div>
  );
}
