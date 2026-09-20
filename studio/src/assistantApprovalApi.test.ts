import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./telegram', () => ({
  getRawInitData: () => 'signed-init-data',
}));

import { studioApi } from './api';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Assistant single approval API', () => {
  it('sends the exact requested content revision for proposal creation', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => ({
      ok: true,
      json: async () => ({}),
    } as Response));
    vi.stubGlobal('fetch', fetchMock);

    await studioApi.createAssistantApproval(
      7,
      101,
      3,
      '14:30',
      'approval-test-0001',
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/channels/7/assistant/approvals');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      content_item_id: 101,
      content_revision: 3,
      local_time: '14:30',
      request_id: 'approval-test-0001',
    });
  });
});
