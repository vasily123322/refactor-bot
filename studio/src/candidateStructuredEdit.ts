import { getRawInitData } from './telegram';
import type { PostDocument } from './types';

export type StructuredEditOperation = 'shorten' | 'expand' | 'to_list' | 'add_headings';

export type StructuredEditResult = {
  candidate_id: number;
  candidate_status: string;
  run_id: number;
  run_status: string;
  provider: string;
  model: string | null;
  text: string;
  reused_existing: boolean;
  output: Record<string, unknown>;
  document: PostDocument;
};

export async function editCurrentStructuredRewrite(
  channelId: number,
  candidateId: number,
  runId: number,
  operation: StructuredEditOperation,
): Promise<StructuredEditResult> {
  const initData = getRawInitData();
  if (!initData) throw new Error('Откройте Studio из Telegram, чтобы авторизоваться.');
  const response = await fetch(
    `/api/studio/channels/${channelId}/candidates/${candidateId}/rewrite/ai/structured/edit`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': initData,
      },
      body: JSON.stringify({ run_id: runId, operation }),
    },
  );
  if (!response.ok) {
    let detail = response.statusText || 'Studio API error';
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Keep status text when the API does not return JSON detail.
    }
    throw new Error(detail);
  }
  return (await response.json()) as StructuredEditResult;
}
