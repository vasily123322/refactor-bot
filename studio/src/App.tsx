import { AppRoot } from '@telegram-apps/telegram-ui';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AIStudioPanel } from './AIStudioPanel';
import { StudioApiError, studioApi } from './api';
import { ChannelOnboardingControl } from './ChannelOnboardingControl';
import { DRAFT_AUTOSAVE_DELAY_MS, isCurrentDraftSave } from './draftAutosave';
import {
  isEditorDocumentDirty,
  reconcileSuccessfulDraftSave,
} from './editorDirty';
import { InboxPanel } from './InboxPanel';
import { PlannerPanel } from './PlannerPanel';
import { RichComposer } from './RichComposer';
import { SourcesPanel } from './SourcesPanel';
import { emitStudioHaptic } from './studioHaptics';
import type { StudioView } from './studioNavigation';
import { TelegramComposer } from './TelegramComposer';
import { TelegramDeliverySettings } from './TelegramDeliverySettings';
import { useTelegramDirtyClosingProtection } from './telegramDirtyClosingProtection';
import { useTelegramStudioBackButton } from './telegramStudioBackButton';
import { TelegramVisualPreview } from './TelegramVisualPreview';
import type {
  Channel,
  ContentDetail,
  ContentSummary,
  PostDocument,
  StudioUser,
} from './types';
import { documentText, emptyRichDocument, emptyTextDocument } from './types';

type DraftSaveState = 'saved' | 'dirty' | 'saving' | 'error';

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

function Sidebar({
  user,
  channels,
  selectedChannelId,
  activeView,
  onSelectChannel,
  onView,
  onChannelsChanged,
}: {
  user: StudioUser | null;
  channels: Channel[];
  selectedChannelId: number | null;
  activeView: StudioView;
  onSelectChannel: (id: number) => void;
  onView: (view: StudioView) => void;
  onChannelsChanged: () => Promise<void>;
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
        <button
          className={activeView === 'sources' ? 'nav-item nav-item-active' : 'nav-item'}
          onClick={() => onView('sources')}
        >
          🔎 Источники
        </button>
        <button
          className={activeView === 'inbox' ? 'nav-item nav-item-active' : 'nav-item'}
          onClick={() => onView('inbox')}
        >
          📥 Inbox
        </button>
        <button
          className={activeView === 'ai' ? 'nav-item nav-item-active' : 'nav-item'}
          onClick={() => onView('ai')}
        >
          ✨ AI Studio
        </button>
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
      <ChannelOnboardingControl onConnected={onChannelsChanged} />
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
  const [saveState, setSaveState] = useState<DraftSaveState>('saved');

  const selectedRef = useRef<ContentDetail | null>(selected);
  const channelIdRef = useRef<number | null>(selectedChannelId);
  const documentRef = useRef<PostDocument>(document);
  const baselineDocumentRef = useRef<PostDocument>(document);
  const dirtyRef = useRef(dirty);
  const editVersionRef = useRef(0);
  const inFlightSaveRef = useRef<Promise<boolean> | null>(null);

  useTelegramDirtyClosingProtection(dirty);
  useTelegramStudioBackButton(view, setView);

  selectedRef.current = selected;
  channelIdRef.current = selectedChannelId;
  documentRef.current = document;
  dirtyRef.current = dirty;

  const channel = useMemo(
    () => channels.find((row) => row.id === selectedChannelId) ?? null,
    [channels, selectedChannelId],
  );

  const loadItems = useCallback(async (channelId: number) => {
    const rows = await studioApi.content(channelId);
    setItems(rows);
  }, []);

  const refreshChannels = useCallback(async () => {
    const nextChannels = await studioApi.channels();
    setChannels(nextChannels);
  }, []);

  const installEditorDocument = useCallback(
    (detail: ContentDetail | null, nextDocument: PostDocument) => {
      editVersionRef.current += 1;
      selectedRef.current = detail;
      documentRef.current = nextDocument;
      baselineDocumentRef.current = structuredClone(nextDocument);
      dirtyRef.current = false;
      setSelected(detail);
      setDocument(nextDocument);
      setDirty(false);
      setSaveState('saved');
    },
    [],
  );

  const persistDraft = useCallback(async (notify = false): Promise<boolean> => {
    while (inFlightSaveRef.current) {
      await inFlightSaveRef.current;
    }
    if (!dirtyRef.current) return true;

    const currentSelected = selectedRef.current;
    const channelId = channelIdRef.current;
    if (!currentSelected || channelId === null) return false;

    const snapshot = {
      channelId,
      contentId: currentSelected.id,
      editVersion: editVersionRef.current,
    };
    const snapshotDocument = documentRef.current;

    const operation = (async (): Promise<boolean> => {
      setSaveState('saving');
      setError(null);
      try {
        const detail = await studioApi.saveRevision(
          snapshot.channelId,
          snapshot.contentId,
          snapshotDocument,
        );
        const targetStillOpen = (
          channelIdRef.current === snapshot.channelId
          && selectedRef.current?.id === snapshot.contentId
        );
        const current = isCurrentDraftSave(snapshot, {
          channelId: channelIdRef.current,
          contentId: selectedRef.current?.id ?? null,
          editVersion: editVersionRef.current,
        });

        if (targetStillOpen) {
          selectedRef.current = detail;
          setSelected(detail);
          const reconciled = reconcileSuccessfulDraftSave(
            detail.document,
            documentRef.current,
          );
          baselineDocumentRef.current = structuredClone(reconciled.baseline);
          if (current) {
            documentRef.current = detail.document;
            dirtyRef.current = false;
            setDocument(detail.document);
            setDirty(false);
            setSaveState('saved');
            if (notify) {
              setNotice(`Сохранена версия ${detail.current_revision}`);
              emitStudioHaptic('manual-save-success');
            }
          } else {
            dirtyRef.current = reconciled.dirty;
            setDirty(reconciled.dirty);
            setSaveState(reconciled.dirty ? 'dirty' : 'saved');
          }
        }
        if (channelIdRef.current === snapshot.channelId) {
          void loadItems(snapshot.channelId).catch((reason) => {
            if (channelIdRef.current === snapshot.channelId) {
              setError(errorMessage(reason));
            }
          });
        }
        return current;
      } catch (reason) {
        const targetStillOpen = (
          channelIdRef.current === snapshot.channelId
          && selectedRef.current?.id === snapshot.contentId
        );
        if (targetStillOpen) {
          setSaveState('error');
          setError(errorMessage(reason));
          if (notify) emitStudioHaptic('manual-save-error');
        }
        return false;
      }
    })();

    inFlightSaveRef.current = operation;
    try {
      return await operation;
    } finally {
      if (inFlightSaveRef.current === operation) {
        inFlightSaveRef.current = null;
      }
    }
  }, [loadItems]);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([studioApi.me(), studioApi.channels()])
      .then(([nextUser, nextChannels]) => {
        if (cancelled) return;
        setUser(nextUser);
        setChannels(nextChannels);
        if (nextChannels.length) {
          channelIdRef.current = nextChannels[0].id;
          setSelectedChannelId(nextChannels[0].id);
        }
      })
      .catch((reason) => !cancelled && setError(errorMessage(reason)));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selectedChannelId === null) return;
    installEditorDocument(null, emptyTextDocument());
    setPreviewMessageIds([]);
    void loadItems(selectedChannelId).catch((reason) => setError(errorMessage(reason)));
  }, [installEditorDocument, loadItems, selectedChannelId]);

  useEffect(() => {
    if (!dirty || !selected || selectedChannelId === null) return;
    const timer = window.setTimeout(() => {
      void persistDraft(false);
    }, DRAFT_AUTOSAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [dirty, document, persistDraft, selected?.id, selectedChannelId]);

  const openContentById = async (contentId: number) => {
    if (channelIdRef.current === null) return;
    if (dirtyRef.current) {
      const saved = await persistDraft(false);
      if (!saved && dirtyRef.current) return;
    }
    const channelId = channelIdRef.current;
    if (channelId === null) return;
    setView('content');
    setBusy(true);
    setError(null);
    try {
      const detail = await studioApi.contentItem(channelId, contentId);
      if (channelIdRef.current !== channelId) return;
      installEditorDocument(detail, detail.document);
      setPreviewMessageIds([]);
      await loadItems(channelId);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const openItem = async (item: ContentSummary) => openContentById(item.id);

  const selectChannel = (channelId: number) => {
    if (channelId === channelIdRef.current) return;
    void (async () => {
      if (dirtyRef.current) {
        const saved = await persistDraft(false);
        if (!saved && dirtyRef.current) return;
      }
      channelIdRef.current = channelId;
      selectedRef.current = null;
      setSelectedChannelId(channelId);
    })();
  };

  const createDraft = async (mode: 'classic' | 'rich') => {
    if (dirtyRef.current) {
      const saved = await persistDraft(false);
      if (!saved && dirtyRef.current) return;
    }
    const channelId = channelIdRef.current;
    if (channelId === null) return;
    setBusy(true);
    setError(null);
    try {
      const initial = mode === 'rich' ? emptyRichDocument() : emptyTextDocument();
      const detail = await studioApi.createContent(
        channelId,
        initial,
        mode === 'rich' ? 'Новый Rich пост' : 'Новый пост',
      );
      if (channelIdRef.current !== channelId) return;
      installEditorDocument(detail, detail.document);
      setPreviewMessageIds([]);
      await loadItems(channelId);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    void persistDraft(true);
  };

  const exactPreview = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await studioApi.telegramPreview(
        documentRef.current,
        previewMessageIds,
        channelIdRef.current,
      );
      setPreviewMessageIds(result.message_ids);
      setNotice('Настоящий preview отправлен в Telegram');
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const publishNow = async () => {
    if (dirtyRef.current) {
      const saved = await persistDraft(false);
      if (!saved && dirtyRef.current) {
        emitStudioHaptic('publish-error');
        setError('Не удалось сохранить последние изменения перед публикацией.');
        return;
      }
    }
    const currentSelected = selectedRef.current;
    const channelId = channelIdRef.current;
    if (!currentSelected || channelId === null) return;
    setBusy(true);
    setError(null);
    try {
      const publication = await studioApi.publishNow(channelId, currentSelected.id);
      setNotice(`Публикация #${publication.id} поставлена в очередь`);
      emitStudioHaptic('publish-success');
    } catch (reason) {
      emitStudioHaptic('publish-error');
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const text = documentText(document);
  const editDocument = (next: PostDocument) => {
    const nextDirty = isEditorDocumentDirty(baselineDocumentRef.current, next);
    editVersionRef.current += 1;
    documentRef.current = next;
    dirtyRef.current = nextDirty;
    setDocument(next);
    setDirty(nextDirty);
    setSaveState(
      nextDirty ? 'dirty' : inFlightSaveRef.current ? 'saving' : 'saved',
    );
  };

  const saveButtonLabel = saveState === 'saving'
    ? 'Сохраняю…'
    : saveState === 'error'
      ? 'Повторить сохранение'
      : dirty
        ? 'Сохранить сейчас'
        : 'Сохранено';
  const saveFooterLabel = saveState === 'saving'
    ? '↻ Автосохранение…'
    : saveState === 'error'
      ? '⚠ Не удалось сохранить'
      : dirty
        ? '● Изменения сохранятся автоматически'
        : '✓ Автосохранено';

  return (
    <AppRoot>
      <div className="app-shell">
        <Sidebar
          user={user}
          channels={channels}
          selectedChannelId={selectedChannelId}
          activeView={view}
          onSelectChannel={selectChannel}
          onView={setView}
          onChannelsChanged={refreshChannels}
        />

        {view === 'planner' ? (
          <main className="workspace planner-page">
            <PlannerPanel channel={channel} onOpenContent={(id) => void openContentById(id)} />
          </main>
        ) : view === 'sources' ? (
          <main className="workspace sources-workspace">
            <SourcesPanel channel={channel} />
          </main>
        ) : view === 'inbox' ? (
          <main className="workspace sources-workspace">
            <InboxPanel channel={channel} onOpenContent={(id) => void openContentById(id)} />
          </main>
        ) : view === 'ai' ? (
          <main className="workspace sources-workspace">
            <AIStudioPanel channel={channel} />
          </main>
        ) : (
          <main className="workspace">
            <header className="topbar">
              <div>
                <small className="eyebrow">{channel?.title || 'Telegram Studio'}</small>
                <h1>{selected ? selected.title || `Пост #${selected.id}` : 'Контент'}</h1>
              </div>
              <div className="top-actions">
                <button className="button secondary" onClick={() => void createDraft('classic')} disabled={busy || !channel}>
                  + Classic
                </button>
                <button className="button secondary" onClick={() => void createDraft('rich')} disabled={busy || !channel}>
                  + Rich
                </button>
                <button className="button secondary" onClick={exactPreview} disabled={busy}>
                  👁 В Telegram
                </button>
                <button className="button primary" onClick={() => void publishNow()} disabled={busy || !selected}>
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
                    <h2>{document.mode === 'rich' ? 'Rich Composer' : 'Composer'}</h2>
                    <small>
                      {selected ? `Content #${selected.id} · revision ${selected.current_revision}` : 'Новый документ'}
                    </small>
                  </div>
                  <button
                    className="button primary compact"
                    onClick={save}
                    disabled={busy || !selected || !dirty || saveState === 'saving'}
                  >
                    {saveButtonLabel}
                  </button>
                </div>
                {document.mode === 'rich' ? (
                  <RichComposer
                    document={document}
                    channelId={selectedChannelId}
                    onChange={editDocument}
                  />
                ) : (
                  <TelegramComposer document={document} onChange={editDocument} />
                )}
                <TelegramDeliverySettings document={document} onChange={editDocument} />
                <footer className="editor-footer">
                  <span>
                    {text.length} UTF-16 единиц · {document.mode === 'rich' ? 'native Rich Message blocks' : 'Telegram text limit: 4096'}
                  </span>
                  <span>{saveFooterLabel}</span>
                </footer>
              </section>

              <section className="preview-panel">
                <div className="panel-heading">
                  <div>
                    <h2>Telegram Preview</h2>
                    <small>Visual + exact Bot API</small>
                  </div>
                </div>
                <TelegramVisualPreview document={document} channel={channel} />
              </section>
            </div>
          </main>
        )}
      </div>
      {busy && <div className="busy-indicator" aria-label="Выполняется операция" />}
    </AppRoot>
  );
}
