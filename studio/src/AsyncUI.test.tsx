import { describe, expect, it } from 'vitest';

import { AsyncRegion, InlineStatus, SkeletonBlock } from './AsyncUI';

describe('AsyncUI', () => {
  it('announces inline background status politely', () => {
    const element = InlineStatus({ children: 'Обновляю…' });

    expect(element).not.toBeNull();
    expect(element!.props.role).toBe('status');
    expect(element!.props['aria-live']).toBe('polite');
    expect(element!.props['aria-atomic']).toBe('true');
  });

  it('keeps skeleton blocks decorative', () => {
    const element = SkeletonBlock({ height: 24, width: '50%' });

    expect(element.props['aria-hidden']).toBe('true');
    expect(element.props.style.height).toBe(24);
    expect(element.props.style.width).toBe('50%');
  });

  it('marks loading regions busy without rendering empty content', () => {
    const element = AsyncRegion({
      loading: true,
      empty: true,
      loadingLabel: 'Загружаю список…',
      loadingFallback: 'skeleton',
      emptyFallback: 'empty',
      children: 'content',
    });

    expect(element.props['aria-busy']).toBe(true);
    const rendered = JSON.stringify(element.props.children);
    expect(rendered).toContain('Загружаю список');
    expect(rendered).toContain('skeleton');
    expect(rendered).not.toContain('empty');
  });
});
