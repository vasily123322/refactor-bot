export const DRAFT_AUTOSAVE_DELAY_MS = 1200;

export type DraftSaveSnapshot = {
  channelId: number;
  contentId: number;
  editVersion: number;
};

export function isCurrentDraftSave(
  snapshot: DraftSaveSnapshot,
  current: {
    channelId: number | null;
    contentId: number | null;
    editVersion: number;
  },
): boolean {
  return (
    current.channelId === snapshot.channelId
    && current.contentId === snapshot.contentId
    && current.editVersion === snapshot.editVersion
  );
}
