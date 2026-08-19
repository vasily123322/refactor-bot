export type StudioView = 'content' | 'planner' | 'sources' | 'inbox' | 'ai';

export const STUDIO_ROOT_VIEW: StudioView = 'content';

export function studioTelegramBackTarget(view: StudioView): StudioView | null {
  return view === STUDIO_ROOT_VIEW ? null : STUDIO_ROOT_VIEW;
}

export function bindStudioTelegramBackNavigation(
  view: StudioView,
  navigate: (target: StudioView) => void,
  bindBackButton: (listener: VoidFunction) => VoidFunction | null,
): VoidFunction | null {
  const target = studioTelegramBackTarget(view);
  if (target === null) return null;
  return bindBackButton(() => navigate(target));
}
