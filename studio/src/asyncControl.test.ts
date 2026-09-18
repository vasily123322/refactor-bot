import { describe, expect, it } from 'vitest';

import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveChannelDataView,
  runExclusiveOperation,
  type ChannelLoadState,
} from './asyncControl';

describe('channel async load ownership', () => {
  it('AI first-load error is not rendered as loaded-empty', () => {
    const state: ChannelLoadState = { channelId: 1, phase: 'error-without-valid-data' };
    expect(resolveChannelDataView(state, 1, 0)).toBe('error-without-valid-data');
  });

  it('AI A→B failed load cannot expose stale A data as B', () => {
    const failedB: ChannelLoadState = { channelId: 2, phase: 'error-without-valid-data' };
    expect(resolveChannelDataView(failedB, 2, 4)).toBe('error-without-valid-data');
  });

  it('AI stale A success/error/finally are ignored after B owns the request slot', () => {
    const ownership = new ChannelRequestOwnership();
    const requestA = ownership.begin(1);
    const requestB = ownership.begin(2);

    expect(ownership.isCurrent(requestA, 2)).toBe(false);
    expect(ownership.isCurrent(requestB, 2)).toBe(true);
    ownership.invalidate();
    expect(ownership.isCurrent(requestB, 2)).toBe(false);
  });

  it('Inbox first-load/A→B failures stay error-without-valid-data even with stale rows in memory', () => {
    const failedB: ChannelLoadState = { channelId: 22, phase: 'error-without-valid-data' };
    expect(resolveChannelDataView(failedB, 22, 0)).toBe('error-without-valid-data');
    expect(resolveChannelDataView(failedB, 22, 7)).toBe('error-without-valid-data');
  });

  it('Inbox stale A completion cannot become current B state', () => {
    const ownership = new ChannelRequestOwnership();
    const requestA = ownership.begin(10);
    const requestB = ownership.begin(20);
    expect(ownership.isCurrent(requestA, 20)).toBe(false);
    expect(ownership.isCurrent(requestB, 20)).toBe(true);
  });

  it('App content load error is distinct from a real loaded-empty list', () => {
    const error: ChannelLoadState = { channelId: 3, phase: 'error-without-valid-data' };
    const loaded: ChannelLoadState = { channelId: 3, phase: 'loaded' };
    expect(resolveChannelDataView(error, 3, 0)).toBe('error-without-valid-data');
    expect(resolveChannelDataView(loaded, 3, 0)).toBe('loaded-empty');
  });
});

describe('App exclusive operation ownership', () => {
  it('gives one winner while the first operation is still in pre-save', async () => {
    const lock = new ExclusiveOperationLock();
    let releaseSave!: () => void;
    const saveGate = new Promise<void>((resolve) => { releaseSave = resolve; });
    let published = 0;
    let opened = 0;

    const publish = runExclusiveOperation(lock, 'publish', async () => {
      await saveGate;
      published += 1;
    });
    const open = await runExclusiveOperation(lock, 'open-content', async () => {
      opened += 1;
    });

    expect(open.started).toBe(false);
    expect(lock.activeKey()).toBe('publish');
    expect(opened).toBe(0);
    releaseSave();
    await publish;
    expect(published).toBe(1);
    expect(lock.isLocked()).toBe(false);
  });

  it('blocks channel switch during pre-publish save and keeps identical keys owned', async () => {
    const lock = new ExclusiveOperationLock();
    let releaseSave!: () => void;
    const saveGate = new Promise<void>((resolve) => { releaseSave = resolve; });

    const publish = runExclusiveOperation(lock, 'publish', async () => {
      await saveGate;
    });
    const duplicatePublish = await runExclusiveOperation(lock, 'publish', async () => undefined);
    const channelSwitch = await runExclusiveOperation(lock, 'select-channel', async () => undefined);

    expect(duplicatePublish.started).toBe(false);
    expect(channelSwitch.started).toBe(false);
    expect(lock.activeKey()).toBe('publish');
    releaseSave();
    await publish;
  });

  it('does not release a newer holder when an old token is released again', () => {
    const lock = new ExclusiveOperationLock();
    const first = lock.tryAcquire('publish');
    expect(first).not.toBeNull();
    expect(lock.release(first!)).toBe(true);

    const second = lock.tryAcquire('open-content');
    expect(second).not.toBeNull();
    expect(lock.release(first!)).toBe(false);
    expect(lock.activeKey()).toBe('open-content');
    expect(lock.release(second!)).toBe(true);
  });
});
