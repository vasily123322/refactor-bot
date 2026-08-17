import { getRawInitData } from './telegram';
import type { PostDocument } from './types';

export type PromptStructuredRewriteResult = {
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

export async function promptCandidateStructuredRewrite(
  channelId: number,
  candidateId: number,
  instruction: string,
): Promise<PromptStructuredRewriteResult> {
  const initData = getRawInitData();
  if (!initData) throw new Error('Откройте Studio из Telegram, чтобы авторизоваться.');
  const response = await fetch(
    `/api/studio/channels/${channelId}/candidates/${candidateId}/rewrite/ai/structured/prompt`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': initData,
      },
      body: JSON.stringify({ instruction }),
    },
  );
  if (!response.ok) {
    let detail = response.statusText || 'Studio API error';
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Keep HTTP status text when no JSON detail is available.
    }
    throw new Error(detail);
  }
  return (await response.json()) as PromptStructuredRewriteResult;
}
