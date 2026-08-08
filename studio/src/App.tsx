import { AppRoot } from '@telegram-apps/telegram-ui';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import { PlannerPanel } from './PlannerPanel';
import { TelegramComposer } from './TelegramComposer';
import type {
  Channel,
  ContentDetail,
  ContentSummary,
  PostDocument,
  StudioUser,
} from './types';
import { documentText, emptyTextDocument } from './types';

type StudioView = 'content' | 'planner';

function shortDate(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? '—'
    : new Intl.DateTimeFormat('ru', {
        day: '2-digit',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      }).format(date);
}

function errorMessage(error: unknown): string {
  if (error instanceof StudioApiError || error instanceof Error) return error.message;
  return 'Неизвестная ошибка';
}

function TelegramSimulator({
  document,
  channel,
}: {
  document: PostDocument;
  channel: Channel | null;
}) {
  const text = documentText(document) || 'Начните писать пост…';
  return (
    <div className="phone-stage" aria-label="Предпросмотр Telegram">
      <div className="phone-header">
        <span className="phone-back">‹</span>
        <div>
          <strong>{channel?.title || 'Telegram Channel'}</strong>
          <small>предпросмотр публикации</small>
        </div>
        <span>•••</span>
      </div>
      <div className="telegram-chat">
        <article className="telegram-post">
          <div className="telegram-avatar">T</div>
          <div className="telegram-body">
            <strong className="telegram-author">{channel?.title || 'Канал'}</strong>
            <div className="telegram-text">{text}</div>
            <div className="telegram-meta">сейчас · 👁 1</div>
          </div>
        </article>
      </div>
      <div className="preview-note">
        Visual preview показывает геометрию и текст. Кнопка «В Telegram» проверяет
        форматирование тем же Bot API renderer, что используется при публикации.
      </div>
    </div>
  );
}

function Sidebar({
  user,
  channels,
  selectedChannelId,
  activeView,
  onSelectChannel,
  onView,
}: {
  user: StudioUser | null;
  channels: Channel[];
  selectedChannelId: number | null;
  activeView: StudioView;
  onSelectChannel: (id: number) => void;
  onView: (view: StudioView) => void;
}) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">TS</span>
        <div>
          <strong>Telegram Studio</strong>
          <small>{user?.full_name || user?.username || 'Content OS'}</small>
        </div>
      </div>

      <nav className="primary-nav" aria-label="Навигация Studio">
        <button
          className={activeView === 'content' ? 'nav-item nav-item-active' : 'nav-item'}
          onClick={() => onView('content')}
        >
          ✍️ Контент
        </button>
        <button
          className={activeView === 'planner' ? 'nav-item nav-item-active' : 'nav-item'}
          onClick={() => onView('planner')}
        >
          📅 Планер
        </button>
        <button className="nav-item" disabled title="Следующий этап">📥 Входящие</button>
        <button className="nav-item" disabled title="Следующий этап">🔎 Источники</button>
        <button className="nav-item" disabled title="Следующий этап">✨ AI Studio</button>
      </nav>

      <div className="sidebar-section-title">Каналы</div>
      <div className="channel-list">
        {channels.map((channel) => (
          <button
            className={
              channel.id === selectedChannelId ? 'channel-button selected' : 'channel-button'
            }
            key={channel.id}
            onClick={() => onSelectChannel(channel.id)}
          >
            <span className="channel-avatar">
              {(channel.title || 'T').trim().slice(0, 1).toUpperCase()}
            </span>
            <span className="channel-label">
              <strong>{channel.title || channel.tg_chat_id}</strong>
              <small>{channel.is_active ? 'активен' : 'отключён'}</small>
            </span>
          </button>
        ))}
      </div>
    </aside>
  );
}

export default function App() {
  const [view, setView] = useState<StudioView>('content');
  const [user, setUser] = useState<StudioUser | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [selectedChannelId, setSelectedChannelId] = useState<number | null>(null);
  const [items, setItems] = useState<ContentSummary[]>([]);
  const [selected, setSelected] = useState<ContentDetail | null>(null);
  const [document, setDocument] = useState<PostDocument>(emptyTextDocument());
  const [previewMessageIds, setPreviewMessageIds] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  const channel = useMemo(
    () => channels.find((row) => row.id === selectedChannelId) ?? null,
    [channels, selectedChannelId],
  );

  const loadItems = useCallback(async (channelId: number) => {
    const rows = await studioApi.content(channelId);
    setItems(rows);
  }, []);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([studioApi.me(), studioApi.channels()])
      .then(([nextUser, nextChannels]) => {
        if (cancelled) return;
        setUser(nextUser);
        setChannels(nextChannels);
        if (nextChannels.length) setSelectedChannelId(nextChannels[0].id);
      })
      .catch((reason) => !cancelled && setError(errorMessage(reason)));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selectedChannelId === null) return;
    setSelected(null);
    setDocument(emptyTextDocument());
    setDirty(false);
    setPreviewMessageIds([]);
    void loadItems(selectedChannelId).catch((reason) => setError(errorMessage(reason)));
  }, [loadItems, selectedChannelId]);

  const openContentById = async (contentId: number) => {
    if (selectedChannelId === null) return;
    setView('content');
    setBusy(true);
    setError(null);
    try {
      const detail = await studioApi.contentItem(selectedChannelId, contentId);
      setSelected(detail);
      setDocument(detail.document);
      setDirty(false);
      setPreviewMessageIds([]);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const openItem = async (item: ContentSummary) => openContentById(item.id);

  const createDraft = async () => {
    if (selectedChannelId === null) return;
    setBusy(true);
    setError(null);
    try {
      const detail = await studioApi.createContent(
        selectedChannelId,
        emptyTextDocument(),
        'Новый пост',
      );
      setSelected(detail);
      setDocument(detail.document);
      setDirty(false);
      await loadItems(selectedChannelId);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!selected || selectedChannelId === null) return;
    setBusy(true);
    setError(null);
    try {
      const detail = await studioApi.saveRevision(selectedChannelId, selected.id, document);
      setSelected(detail);
      setDocument(detail.document);
      setDirty(false);
      setNotice(`Сохранена версия ${detail.current_revision}`);
      await loadItems(selectedChannelId);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const exactPreview = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await studioApi.telegramPreview(document, previewMessageIds);
      setPreviewMessageIds(result.message_ids);
      setNotice('Настоящий preview отправлен в Telegram');
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const publishNow = async () => {
    if (!selected || selectedChannelId === null) return;
    if (dirty) {
      setError('Сначала сохраните текущие изменения как новую версию.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const publication = await studioApi.publishNow(selectedChannelId, selected.id);
      setNotice(`Публикация #${publication.id} поставлена в очередь`);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const text = documentText(document);
  const editDocument = (next: PostDocument) => {
    setDocument(next);
    setDirty(true);
  };

  return (
    <AppRoot>
      <div className="app-shell">
        <Sidebar
          user={user}
          channels={channels}
          selectedChannelId={selectedChannelId}
          activeView={view}
          onSelectChannel={setSelectedChannelId}
          onView={setView}
        />

        {view === 'planner' ? (
          <main className="workspace planner-page">
            <PlannerPanel channel={channel} onOpenContent={(id) => void openContentById(id)} />
          </main>
        ) : (
          <main className="workspace">
            <header className="topbar">
              <div>
                <small className="eyebrow">{channel?.title || 'Telegram Studio'}</small>
                <h1>{selected ? selected.title || `Пост #${selected.id}` : 'Контент'}</h1>
              </div>
              <div className="top-actions">
                <button className="button secondary" onClick={createDraft} disabled={busy || !channel}>
                  + Новый пост
                </button>
                <button className="button secondary" onClick={exactPreview} disabled={busy}>
                  👁 В Telegram
                </button>
                <button className="button primary" onClick={publishNow} disabled={busy || !selected}>
                  Опубликовать
                </button>
              </div>
            </header>

            {error && (
              <div className="banner error" role="alert">
                {error}
                <button onClick={() => setError(null)}>×</button>
              </div>
            )}
            {notice && (
              <div className="banner success">
                {notice}
                <button onClick={() => setNotice(null)}>×</button>
              </div>
            )}

            <div className="studio-grid">
              <section className="content-panel">
                <div className="panel-heading">
                  <div>
                    <h2>Публикации</h2>
                    <small>{items.length} объектов в Content domain</small>
                  </div>
                </div>
                <div className="content-list">
                  {items.length === 0 && <div className="empty-state">Создайте первый пост.</div>}
                  {items.map((item) => (
                    <button
                      key={item.id}
                      className={selected?.id === item.id ? 'content-row selected' : 'content-row'}
                      onClick={() => void openItem(item)}
                    >
                      <span>
                        <strong>{item.title || `Пост #${item.id}`}</strong>
                        <small>{shortDate(item.updated_at)} · v{item.current_revision}</small>
                      </span>
                      <span className={`status status-${item.status}`}>{item.status}</span>
                    </button>
                  ))}
                </div>
              </section>

              <section className="editor-panel">
                <div className="panel-heading editor-heading">
                  <div>
                    <h2>Composer</h2>
                    <small>
                      {selected ? `Content #${selected.id} · revision ${selected.current_revision}` : 'Новый документ'}
                    </small>
                  </div>
                  <button className="button primary compact" onClick={save} disabled={busy || !selected || !dirty}>
                    {dirty ? 'Сохранить версию' : 'Сохранено'}
                  </button>
                </div>
                <TelegramComposer document={document} onChange={editDocument} />
                <footer className="editor-footer">
                  <span>{text.length} UTF-16 единиц · Telegram limit: 4096 для text</span>
                  <span>{dirty ? '● Есть несохранённые изменения' : '✓ Версия сохранена'}</span>
                </footer>
              </section>

              <section className="preview-panel">
                <div className="panel-heading">
                  <div>
                    <h2>Telegram Preview</h2>
                    <small>Visual + exact Bot API</small>
                  </div>
                </div>
                <TelegramSimulator document={document} channel={channel} />
              </section>
            </div>
          </main>
        )}
      </div>
      {busy && <div className="busy-indicator" aria-label="Выполняется операция" />}
    </AppRoot>
  );
}
