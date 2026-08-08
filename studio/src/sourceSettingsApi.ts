import { StudioApiError } from './api';
import { getRawInitData } from './telegram';
import type { SourceConnectorView } from './types';

export type SourceSettingsPatch = {
  enabled?: boolean;
  mode?: 'summary' | 'rewrite';
  citation_enabled?: boolean;
  reuse_policy?:
    | 'reference_only'
    | 'summarize'
    | 'quote_with_attribution'
    | 'rewrite_with_attribution'
    | 'mirror_authorized';
};

export async function updateSourceSettings(
  channelId: number,
  connectorId: number,
  patch: SourceSettingsPatch,
): Promise<SourceConnectorView> {
  const initData = getRawInitData();
  if (!initData) {
    throw new StudioApiError('Откройте Studio из Telegram, чтобы авторизоваться.', 401);
  }
  const response = await fetch(
    `/api/studio/channels/${channelId}/sources/${connectorId}/settings`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': initData,
      },
      body: JSON.stringify(patch),
    },
  );
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Keep status text for non-JSON failures.
    }
    throw new StudioApiError(detail || 'Source settings update failed', response.status);
  }
  return (await response.json()) as SourceConnectorView;
}
