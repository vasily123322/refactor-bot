import { describe, expect, it, vi } from 'vitest';

import {
  bindStudioTelegramBackNavigation,
  STUDIO_ROOT_VIEW,
  studioTelegramBackTarget,
} from './studioNavigation';
import type { StudioView } from './studioNavigation';

describe('Studio Telegram BackButton navigation', () => {
  it('treats content as the navigation root and does not bind a BackButton there', () => {
    const navigate = vi.fn();
    const bind = vi.fn((_listener: VoidFunction) => vi.fn());
    expect(studioTelegramBackTarget('content')).toBeNull();
    expect(bindStudioTelegramBackNavigation('content', navigate, bind)).toBeNull();
    expect(bind).not.toHaveBeenCalled();
  });

  it.each<StudioView>(['planner', 'sources', 'inbox', 'ai'])(
    'maps nested %s view back to the existing content root',
    (view) => {
      expect(studioTelegramBackTarget(view)).toBe(STUDIO_ROOT_VIEW);
    },
  );

  it('uses the existing navigate callback instead of maintaining a parallel history stack', () => {
    const navigate = vi.fn();
    const dispose = vi.fn();
    const bind = vi.fn((_next: VoidFunction) => dispose);

    expect(bindStudioTelegramBackNavigation('sources', navigate, bind)).toBe(dispose);
    expect(bind).toHaveBeenCalledTimes(1);
    const listener = bind.mock.calls[0]?.[0];
    expect(listener).toBeTypeOf('function');
    listener?.();
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith('content');
  });

  it('forwards the native button disposer so view changes/unmount release ownership', () => {
    const dispose = vi.fn();
    const binding = bindStudioTelegramBackNavigation(
      'planner',
      vi.fn(),
      vi.fn(() => dispose),
    );
    binding?.();
    expect(dispose).toHaveBeenCalledTimes(1);
  });

  it('does not invent a back target inside the existing content/editor surface', () => {
    expect(studioTelegramBackTarget('content')).toBeNull();
  });
});
