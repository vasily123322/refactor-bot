import { AppRoot } from '@telegram-apps/telegram-ui';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AIStudioPanel } from './AIStudioPanel';
import { AsyncRegion, InlineStatus, SkeletonBlock } from './AsyncUI';
import {
  ExclusiveOperationLock,
  resolveChannelDataView,
  runExclusiveOperation,
  type ChannelLoadState,
} from './asyncControl';
import { StudioApiError, studioApi } from './api';
import { ChannelOnboardingControl } from './ChannelOnboardingControl';
import { runComposerPreviewOnce } from './composerPreviewAction';
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
import { useTelegramComposerMainButton } from './telegramComposerMainButton';
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
  loading,
  channelSelectionDisabled,
}: {
  user: StudioUser | null;
  channels: Channel[];
  selectedChannelId: number | null;
  activeView: StudioView;
  onSelectChannel: (id: number) => void;
  onView: (view: StudioView) => void;
  onChannelsChanged: () => Promise<void>;
  loading: boolean;
  channelSelectionDisabled: boolean;
}) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">TS</span>
        <div>
          <strong>Telegram Studio</strong>
          <small>{loading ? 'Загрузка…' : user?.full_name || user?.username || 'Content OS'}</small>
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
      <div className="channel-list" aria-busy={loading || undefined}>
        {loading ? (
          <>
            <span className="sr-only" role="status" aria-live="polite">Загружаю каналы…</span>
            {[0, 1, 2].map((index) => (
              <div className="content-row-skeleton" key={index} aria-hidden="true">
                <SkeletonBlock height={12} width="70%" />
                <SkeletonBlock height={9} width="45%" />
              </div>
            ))}
          </>
        ) : channels.map((channel) => (
          <button
            className={
              channel.id === selectedChannelId ? 'channel-button selected' : 'channel-button'
            }
            key={channel.id}
            onClick={() => onSelectChannel(channel.id)}
            disabled={channelSelectionDisabled}
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
  const [itemsLoadState, setItemsLoadState] = useState<ChannelLoadState | null>(null);
  const [selected, setSelected] = useState<ContentDetail | null>(null);
  const [document, setDocument] = useState<PostDocument>(emptyTextDocument());
  const [previewMessageIds, setPreviewMessageIds] = useState<number[]>([]);
  const [initialLoading, setInitialLoading] = useState(true);
  const [bootstrapLoadFailed, setBootstrapLoadFailed] = useState(false);
  const [activeOperations, setActiveOperations] = useState<Set<string>>(() => new Set());
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
  const inFlightPreviewRef = useRef<Promise<void> | null>(null);
  const loadedItemsChannelRef = useRef<number | null>(null);
  const operationLockRef = useRef(new ExclusiveOperationLock());

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
    const hasValidData = loadedItemsChannelRef.current === channelId;
    if (channelIdRef.current === channelId && !hasValidData) {
      setItemsLoadState({ channelId, phase: 'loading' });
    }
    try {
      const rows = await studioApi.content(channelId);
      if (channelIdRef.current !== channelId) return;
      loadedItemsChannelRef.current = channelId;
      setItems(rows);
      setItemsLoadState({ channelId, phase: 'loaded' });
    } catch (reason) {
      if (channelIdRef.current === channelId && loadedItemsChannelRef.current !== channelId) {
        setItems([]);
        setItemsLoadState({ channelId, phase: 'error-without-valid-data' });
      }
      throw reason;
    }
  }, []);

  const runOperation = useCallback(async <T,>(
    key: string,
    operation: () => Promise<T>,
  ): Promise<T | undefined> => {
    const result = await runExclusiveOperation(
      operationLockRef.current,
      key,
      operation,
      (activeKey) => setActiveOperations(activeKey ? new Set([activeKey]) : new Set()),
    );
    return result.started ? result.value : undefined;
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
        setBootstrapLoadFailed(false);
        setUser(nextUser);
        setChannels(nextChannels);
        if (nextChannels.length) {
          channelIdRef.current = nextChannels[0].id;
          setSelectedChannelId(nextChannels[0].id);
        }
      })
      .catch((reason) => {
        if (cancelled) return;
        setBootstrapLoadFailed(true);
        setError(errorMessage(reason));
      })
      .finally(() => {
        if (!cancelled) setInitialLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selectedChannelId === null) {
      loadedItemsChannelRef.current = null;
      setItems([]);
      setItemsLoadState(null);
      return;
    }
    installEditorDocument(null, emptyTextDocument());
    setPreviewMessageIds([]);
    loadedItemsChannelRef.current = null;
    setItems([]);
    setItemsLoadState({ channelId: selectedChannelId, phase: 'loading' });
    void loadItems(selectedChannelId).catch((reason) => {
      if (channelIdRef.current === selectedChannelId) {
        setError(errorMessage(reason));
      }
    });
  }, [installEditorDocument, loadItems, selectedChannelId]);

  useEffect(() => {
    if (!dirty || !selected || selectedChannelId === null) return;
    const timer = window.setTimeout(() => {
      void persistDraft(false);
    }, DRAFT_AUTOSAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [dirty, document, persistDraft, selected?.id, selectedChannelId]);

  const openContentById = async (contentId: number) => {
    await runOperation('open-content', async () => {
      const channelId = channelIdRef.current;
      if (channelId === null) return;
      if (dirtyRef.current) {
        const saved = await persistDraft(false);
        if (!saved && dirtyRef.current) return;
      }
      if (channelIdRef.current !== channelId) return;
      setView('content');
      setError(null);
      try {
        const detail = await studioApi.contentItem(channelId, contentId);
        if (channelIdRef.current !== channelId) return;
        installEditorDocument(detail, detail.document);
        setPreviewMessageIds([]);
        await loadItems(channelId);
      } catch (reason) {
        if (channelIdRef.current === channelId) setError(errorMessage(reason));
      }
    });
  };

  const openItem = async (item: ContentSummary) => openContentById(item.id);

  const selectChannel = (channelId: number) => {
    if (channelId === channelIdRef.current) return;
    void runOperation('select-channel', async () => {
      if (channelId === channelIdRef.current) return;
      if (dirtyRef.current) {
        const saved = await persistDraft(false);
        if (!saved && dirtyRef.current) return;
      }
      channelIdRef.current = channelId;
      selectedRef.current = null;
      loadedItemsChannelRef.current = null;
      setItems([]);
      setSelectedChannelId(channelId);
    });
  };

  const createDraft = async (mode: 'classic' | 'rich') => {
    await runOperation('create-draft', async () => {
      if (dirtyRef.current) {
        const saved = await persistDraft(false);
        if (!saved && dirtyRef.current) return;
      }
      const channelId = channelIdRef.current;
      if (channelId === null) return;
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
        if (channelIdRef.current === channelId) setError(errorMessage(reason));
      }
    });
  };

  const save = () => {
    void persistDraft(true);
  };

  const exactPreview = useCallback((): Promise<void> => runComposerPreviewOnce(
    inFlightPreviewRef,
    async () => {
      await runOperation('telegram-preview', async () => {
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
        }
      });
    },
  ), [previewMessageIds, runOperation]);

  const publishNow = async () => {
    await runOperation('publish', async () => {
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
      setError(null);
      try {
        const publication = await studioApi.publishNow(channelId, currentSelected.id);
        setNotice(`Публикация #${publication.id} поставлена в очередь`);
        emitStudioHaptic('publish-success');
      } catch (reason) {
        emitStudioHaptic('publish-error');
        setError(errorMessage(reason));
      }
    });
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
  const telegramPreviewActionLabel = '👁 В Telegram';
  const openingContent = activeOperations.has('open-content');
  const creatingDraft = activeOperations.has('create-draft');
  const switchingChannel = activeOperations.has('select-channel');
  const previewing = activeOperations.has('telegram-preview');
  const publishing = activeOperations.has('publish');
  const editorTransitionBusy = openingContent || creatingDraft || switchingChannel;
  const contentDataView = bootstrapLoadFailed
    ? 'error-without-valid-data'
    : selectedChannelId === null
      ? 'loaded-empty'
      : resolveChannelDataView(itemsLoadState, selectedChannelId, items.length);
  const contentInitialLoading = initialLoading
    || (selectedChannelId !== null && contentDataView === 'loading');
  const contentLoadFailed = contentDataView === 'error-without-valid-data';
  const contentEmpty = !initialLoading && contentDataView === 'loaded-empty';

  useTelegramComposerMainButton({
    active: view === 'content',
    label: telegramPreviewActionLabel,
    disabled: previewing || editorTransitionBusy || publishing,
    loading: previewing,
    onSubmit: exactPreview,
  });

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
          loading={initialLoading}
          channelSelectionDisabled={editorTransitionBusy || previewing || publishing}
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
                <button className="button secondary" onClick={() => void createDraft('classic')} disabled={editorTransitionBusy || previewing || publishing || !channel}>
                  + Classic
                </button>
                <button className="button secondary" onClick={() => void createDraft('rich')} disabled={editorTransitionBusy || previewing || publishing || !channel}>
                  + Rich
                </button>
                <button className="button secondary" onClick={exactPreview} disabled={previewing || editorTransitionBusy || publishing}>
                  {telegramPreviewActionLabel}
                </button>
                <button className="button primary" onClick={() => void publishNow()} disabled={publishing || editorTransitionBusy || previewing || !selected}>
                  Опубликовать
                </button>
                {creatingDraft && <InlineStatus>Создаю новый черновик…</InlineStatus>}
                {openingContent && <InlineStatus>Открываю публикацию…</InlineStatus>}
                {switchingChannel && <InlineStatus>Сохраняю и переключаю канал…</InlineStatus>}
                {previewing && <InlineStatus>Отправляю preview в Telegram…</InlineStatus>}
                {publishing && <InlineStatus>Ставлю публикацию в очередь…</InlineStatus>}
              </div>
            </header>

            {error && (
              <div className="banner error" role="alert">
                {error}
                <button onClick={() => setError(null)}>×</button>
              </div>
            )}
            {notice && (
              <InlineStatus className="banner success">
                {notice}
                <button onClick={() => setNotice(null)}>×</button>
              </InlineStatus>
            )}

            <div className="studio-grid">
              <section className="content-panel">
                <div className="panel-heading">
                  <div>
                    <h2>Публикации</h2>
                    <small>{contentInitialLoading ? 'Загружаю Content domain…' : contentLoadFailed ? 'Content domain не загружен' : `${items.length} объектов в Content domain`}</small>
                  </div>
                </div>
                <AsyncRegion
                  className="content-list"
                  loading={contentInitialLoading}
                  error={contentLoadFailed}
                  empty={contentEmpty}
                  loadingLabel="Загружаю публикации…"
                  loadingFallback={
                    <div className="content-list-skeleton">
                      {[0, 1, 2, 3].map((index) => (
                        <div className="content-row-skeleton" key={index}>
                          <SkeletonBlock height={12} width={index % 2 ? '58%' : '74%'} />
                          <SkeletonBlock height={9} width="42%" />
                        </div>
                      ))}
                    </div>
                  }
                  emptyFallback={
                    <div className="empty-state">
                      {channel ? 'Создайте первый пост.' : 'Подключите или выберите канал.'}
                    </div>
                  }
                  errorFallback={
                    <div className="empty-state">Публикации не загружены. Повторите попытку.</div>
                  }
                >
                  {items.map((item) => (
                    <button
                      key={item.id}
                      className={selected?.id === item.id ? 'content-row selected' : 'content-row'}
                      onClick={() => void openItem(item)}
                      disabled={editorTransitionBusy || previewing || publishing}
                    >
                      <span>
                        <strong>{item.title || `Пост #${item.id}`}</strong>
                        <small>{shortDate(item.updated_at)} · v{item.current_revision}</small>
                      </span>
                      <span className={`status status-${item.status}`}>{item.status}</span>
                    </button>
                  ))}
                </AsyncRegion>
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
                    disabled={editorTransitionBusy || publishing || !selected || !dirty || saveState === 'saving'}
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
                  <InlineStatus>{saveFooterLabel}</InlineStatus>
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
    </AppRoot>
  );
}
