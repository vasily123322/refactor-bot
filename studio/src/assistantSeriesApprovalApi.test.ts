import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./telegram', () => ({
  getRawInitData: () => 'signed-init-data',
}));

import { studioApi } from './api';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Assistant series scheduling API', () => {
  it('sends only source run, request id and ordinal/date/time slots for proposal creation', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({}),
    } as Response));
    vi.stubGlobal('fetch', fetchMock);

    const slots = [
      { ordinal: 1, local_date: '2026-09-20', local_time: '13:00' },
      { ordinal: 2, local_date: '2026-09-20', local_time: '14:30' },
    ];

    await studioApi.createAssistantSeriesApproval(
      7,
      33,
      slots,
      'series-approval-test-0001',
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/studio/channels/7/assistant/series-approvals');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      source_run_id: 33,
      request_id: 'series-approval-test-0001',
      slots,
    });
  });

  it('approve and reject send no execution plan', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({}),
    } as Response));
    vi.stubGlobal('fetch', fetchMock);

    await studioApi.approveAssistantSeriesApproval(7, 81);
    await studioApi.rejectAssistantSeriesApproval(7, 81);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({});
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({});
  });
});
