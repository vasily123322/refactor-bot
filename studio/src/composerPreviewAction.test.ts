import { describe, expect, it, vi } from 'vitest';

import { runComposerPreviewOnce } from './composerPreviewAction';

describe('Composer preview single-flight guard', () => {
  it('converges near-simultaneous HTML/native invocation on one operation', async () => {
    let resolve!: () => void;
    const pending = new Promise<void>((done) => {
      resolve = done;
    });
    const operation = vi.fn(() => pending);
    const inFlight = { current: null as Promise<void> | null };

    const first = runComposerPreviewOnce(inFlight, operation);
    const second = runComposerPreviewOnce(inFlight, operation);

    expect(first).toBe(pending);
    expect(second).toBe(pending);
    expect(operation).toHaveBeenCalledTimes(1);

    resolve();
    await pending;
    await Promise.resolve();
    expect(inFlight.current).toBeNull();
  });

  it('allows a new intentional preview after the previous operation settles', async () => {
    const operation = vi.fn(async () => undefined);
    const inFlight = { current: null as Promise<void> | null };

    await runComposerPreviewOnce(inFlight, operation);
    await Promise.resolve();
    await runComposerPreviewOnce(inFlight, operation);

    expect(operation).toHaveBeenCalledTimes(2);
  });

  it('clears the guard after a rejected operation', async () => {
    const operation = vi.fn()
      .mockRejectedValueOnce(new Error('preview failed'))
      .mockResolvedValueOnce(undefined);
    const inFlight = { current: null as Promise<void> | null };

    await expect(runComposerPreviewOnce(inFlight, operation)).rejects.toThrow('preview failed');
    await Promise.resolve();
    expect(inFlight.current).toBeNull();

    await runComposerPreviewOnce(inFlight, operation);
    expect(operation).toHaveBeenCalledTimes(2);
  });
});
