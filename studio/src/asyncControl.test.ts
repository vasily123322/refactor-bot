import { describe, expect, it } from 'vitest';

import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveChannelDataView,
  resolveScopedDataView,
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

  it('Planner stale response is ignored after channel or week ownership changes', () => {
    const ownership = new ChannelRequestOwnership();
    const oldWeek = ownership.begin(10, '2026-09-14');
    const newWeek = ownership.begin(10, '2026-09-21');

    expect(ownership.isCurrent(oldWeek, 10, '2026-09-21')).toBe(false);
    expect(ownership.isCurrent(newWeek, 10, '2026-09-21')).toBe(true);

    const nextChannel = ownership.begin(20, '2026-09-21');
    expect(ownership.isCurrent(newWeek, 20, '2026-09-21')).toBe(false);
    expect(ownership.isCurrent(nextChannel, 20, '2026-09-21')).toBe(true);
  });

  it('Planner zero rows stay loading until the current scope has loaded', () => {
    expect(resolveScopedDataView(null, 10, '2026-09-21', 0)).toBe('loading');
    expect(resolveScopedDataView(
      { channelId: 10, scopeKey: '2026-09-14', phase: 'loaded' },
      10,
      '2026-09-21',
      0,
    )).toBe('loading');
    expect(resolveScopedDataView(
      { channelId: 10, scopeKey: '2026-09-21', phase: 'loaded' },
      10,
      '2026-09-21',
      0,
    )).toBe('loaded-empty');
  });

  it('Sources stale channel response cannot overwrite the current channel', () => {
    const ownership = new ChannelRequestOwnership();
    const requestA = ownership.begin(1);
    const requestB = ownership.begin(2);

    expect(ownership.isCurrent(requestA, 2)).toBe(false);
    expect(ownership.isCurrent(requestB, 2)).toBe(true);
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

  it('Planner reschedule/cancel share one schedule lock without blocking another schedule', async () => {
    const scheduleOne = new ExclusiveOperationLock();
    const scheduleTwo = new ExclusiveOperationLock();
    let releaseMove!: () => void;
    const moveGate = new Promise<void>((resolve) => { releaseMove = resolve; });
    let cancellations = 0;
    let unrelatedOperations = 0;

    const reschedule = runExclusiveOperation(scheduleOne, 'reschedule:1', async () => {
      await moveGate;
    });
    const conflictingCancel = await runExclusiveOperation(scheduleOne, 'cancel:1', async () => {
      cancellations += 1;
    });
    const unrelatedCancel = await runExclusiveOperation(scheduleTwo, 'cancel:2', async () => {
      unrelatedOperations += 1;
    });

    expect(conflictingCancel.started).toBe(false);
    expect(cancellations).toBe(0);
    expect(unrelatedCancel.started).toBe(true);
    expect(unrelatedOperations).toBe(1);

    releaseMove();
    await reschedule;
    expect(scheduleOne.isLocked()).toBe(false);
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
