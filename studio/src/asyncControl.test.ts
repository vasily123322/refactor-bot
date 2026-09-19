import { describe, expect, it } from 'vitest';

import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  ScopedExclusiveOperationLock,
  resolveChannelDataView,
  resolveScopedDataView,
  runExclusiveOperation,
  runScopedExclusiveOperation,
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

  it('rejects an old A completion after an A→B→A selection cycle', () => {
    const ownership = new ChannelRequestOwnership();
    const firstA = ownership.begin(1, 'selection');
    ownership.begin(2, 'selection');
    const secondA = ownership.begin(1, 'selection');

    expect(ownership.isCurrent(firstA, 1, 'selection')).toBe(false);
    expect(ownership.isCurrent(secondA, 1, 'selection')).toBe(true);
  });

  it('App content load error is distinct from a real loaded-empty list', () => {
    const error: ChannelLoadState = { channelId: 3, phase: 'error-without-valid-data' };
    const loaded: ChannelLoadState = { channelId: 3, phase: 'loaded' };
    expect(resolveChannelDataView(error, 3, 0)).toBe('error-without-valid-data');
    expect(resolveChannelDataView(loaded, 3, 0)).toBe('loaded-empty');
  });

  it('App same-channel content refresh gives ownership to the newest request', () => {
    const ownership = new ChannelRequestOwnership();
    const first = ownership.begin(3);
    const second = ownership.begin(3);

    expect(ownership.isCurrent(first, 3)).toBe(false);
    expect(ownership.isCurrent(second, 3)).toBe(true);
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

  it('Sources serialize one connector mutation without blocking another connector', async () => {
    const sourceOne = new ExclusiveOperationLock();
    const sourceTwo = new ExclusiveOperationLock();
    let releaseDoctor!: () => void;
    const doctorGate = new Promise<void>((resolve) => { releaseDoctor = resolve; });
    let conflictingSettings = 0;
    let unrelatedIngest = 0;

    const doctor = runExclusiveOperation(sourceOne, 'doctor:1', async () => {
      await doctorGate;
    });
    const settings = await runExclusiveOperation(sourceOne, 'settings:1', async () => {
      conflictingSettings += 1;
    });
    const ingest = await runExclusiveOperation(sourceTwo, 'ingest:2', async () => {
      unrelatedIngest += 1;
    });

    expect(settings.started).toBe(false);
    expect(conflictingSettings).toBe(0);
    expect(ingest.started).toBe(true);
    expect(unrelatedIngest).toBe(1);

    releaseDoctor();
    await doctor;
    expect(sourceOne.isLocked()).toBe(false);
  });

  it('Inbox scopes conflicting candidate operations without blocking another candidate', async () => {
    const lock = new ScopedExclusiveOperationLock();
    let releaseFirst!: () => void;
    const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
    let duplicate = 0;
    let unrelated = 0;

    const first = runScopedExclusiveOperation(lock, 'candidate:10', 'enrich-ai:10', async () => {
      await firstGate;
    });
    const conflicting = await runScopedExclusiveOperation(lock, 'candidate:10', 'draft:10', async () => {
      duplicate += 1;
    });
    const other = await runScopedExclusiveOperation(lock, 'candidate:11', 'draft:11', async () => {
      unrelated += 1;
    });

    expect(conflicting.started).toBe(false);
    expect(duplicate).toBe(0);
    expect(other.started).toBe(true);
    expect(unrelated).toBe(1);

    releaseFirst();
    await first;
  });

  it('Inbox global operations conflict with any candidate operation', async () => {
    const lock = new ScopedExclusiveOperationLock();
    let releaseCandidate!: () => void;
    const candidateGate = new Promise<void>((resolve) => { releaseCandidate = resolve; });
    let batchRuns = 0;

    const candidate = runScopedExclusiveOperation(lock, 'candidate:10', 'rewrite-ai:10', async () => {
      await candidateGate;
    });
    const batch = await runScopedExclusiveOperation(lock, null, 'batch-local', async () => {
      batchRuns += 1;
    });

    expect(batch.started).toBe(false);
    expect(batchRuns).toBe(0);
    releaseCandidate();
    await candidate;

    const refresh = runScopedExclusiveOperation(lock, null, 'refresh', async () => undefined);
    const blockedCandidate = await runScopedExclusiveOperation(lock, 'candidate:10', 'draft:10', async () => undefined);
    expect(blockedCandidate.started).toBe(false);
    expect((await refresh).started).toBe(true);
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
