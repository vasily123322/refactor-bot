import { describe, expect, it } from 'vitest';

import { ChannelRequestOwnership } from './asyncControl';
import {
  inboxStatusPresentation,
  inboxStatusScope,
} from './inboxCandidateStatus';

describe('Inbox candidate status views', () => {
  it('gives same-channel status switches distinct request ownership epochs', () => {
    const ownership = new ChannelRequestOwnership();
    const firstNew = ownership.begin(7, inboxStatusScope('new'));
    const dismissed = ownership.begin(7, inboxStatusScope('dismissed'));

    expect(
      ownership.isCurrent(firstNew, 7, inboxStatusScope('new')),
    ).toBe(false);
    expect(
      ownership.isCurrent(dismissed, 7, inboxStatusScope('dismissed')),
    ).toBe(true);

    const secondNew = ownership.begin(7, inboxStatusScope('new'));
    expect(
      ownership.isCurrent(dismissed, 7, inboxStatusScope('dismissed')),
    ).toBe(false);
    expect(
      ownership.isCurrent(secondNew, 7, inboxStatusScope('new')),
    ).toBe(true);
  });

  it('rejects a stale dismissed mutation after returning to the new view', () => {
    const ownership = new ChannelRequestOwnership();
    const restore = ownership.begin(7, inboxStatusScope('dismissed'));
    ownership.begin(7, inboxStatusScope('new'));

    expect(
      ownership.isCurrent(restore, 7, inboxStatusScope('new')),
    ).toBe(false);
  });

  it('shows restore-only mutations for dismissed candidates', () => {
    expect(inboxStatusPresentation('dismissed')).toMatchObject({
      heading: 'Скрытые кандидаты',
      showActiveActions: false,
      showRestore: true,
    });
    expect(inboxStatusPresentation('new')).toMatchObject({
      heading: 'Новые кандидаты',
      showActiveActions: true,
      showRestore: false,
    });
  });
});
