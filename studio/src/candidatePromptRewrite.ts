import { studioRequest } from './api';
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
  return studioRequest<PromptStructuredRewriteResult>(
    `/api/studio/channels/${channelId}/candidates/${candidateId}/rewrite/ai/structured/prompt`,
    {
      method: 'POST',
      body: JSON.stringify({ instruction }),
    },
  );
}
