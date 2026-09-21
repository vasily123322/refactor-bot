import { studioRequest } from './api';
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
  return studioRequest<StructuredEditResult>(
    `/api/studio/channels/${channelId}/candidates/${candidateId}/rewrite/ai/structured/edit`,
    {
      method: 'POST',
      body: JSON.stringify({ run_id: runId, operation }),
    },
  );
}
