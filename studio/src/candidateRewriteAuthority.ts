import { StudioApiError, studioRequest } from './api';
import type { ContentCandidateView, ContentDetail, PostDocument } from './types';

export type RewriteProvenanceStatus =
  | 'current_at_last_server_check'
  | 'stale'
  | 'historical'
  | 'failed'
  | 'unavailable';

export type RewritePreview = {
  runId: number;
  text: string;
  provider?: string | null;
  model: string | null;
  kind: 'text' | 'structured';
  document: PostDocument | null;
  provenanceStatus?: RewriteProvenanceStatus;
};

export type CurrentStructuredRewrite = {
  candidate_id: number;
  run_id: number;
  provider: string;
  model: string | null;
  text: string;
  document: PostDocument;
};

const provenanceStatusLabel: Record<RewriteProvenanceStatus, string> = {
  current_at_last_server_check: 'current at last server check',
  stale: 'stale',
  historical: 'historical',
  failed: 'failed',
  unavailable: 'cannot verify',
};

export function rewriteProvenanceLabel(
  preview: RewritePreview | null | undefined,
  candidateId: number,
  sourceDocumentId?: number | null,
): string | null {
  if (!preview || preview.kind !== 'structured') return null;
  const status = preview.provenanceStatus || 'unavailable';
  const source = sourceDocumentId == null ? '' : ` · source #${sourceDocumentId}`;
  const provider = preview.provider ? ` · provider ${preview.provider}` : '';
  const model = preview.model ? ` · model ${preview.model}` : '';
  return `AI Rich proposal · ${provenanceStatusLabel[status]} · candidate #${candidateId}${source} · run #${preview.runId}${provider}${model}`;
}

export function isRewriteAuthorityStaleError(error: unknown): boolean {
  return error instanceof StudioApiError && error.status === 409;
}

export function previewFromCurrentStructuredRewrite(
  result: CurrentStructuredRewrite,
): RewritePreview {
  return {
    runId: result.run_id,
    text: result.text,
    provider: result.provider,
    model: result.model,
    kind: 'structured',
    document: result.document,
    provenanceStatus: 'current_at_last_server_check',
  };
}

export async function currentCandidateStructuredRewrite(
  channelId: number,
  candidateId: number,
): Promise<CurrentStructuredRewrite | null> {
  return studioRequest<CurrentStructuredRewrite | null>(
    `/api/studio/channels/${channelId}/candidates/${candidateId}/rewrite/ai/structured/current`,
  );
}

export async function loadCurrentStructuredRewritePreviews(
  channelId: number,
  candidates: ContentCandidateView[],
  loadCurrent: (
    channelId: number,
    candidateId: number,
  ) => Promise<CurrentStructuredRewrite | null> = currentCandidateStructuredRewrite,
): Promise<Record<number, RewritePreview>> {
  const recoverable = candidates.filter(
    (candidate) => candidate.reuse_policy === 'rewrite_with_attribution',
  );
  const entries = await Promise.all(
    recoverable.map(async (candidate) => {
      try {
        const current = await loadCurrent(channelId, candidate.id);
        return current
          ? ([candidate.id, previewFromCurrentStructuredRewrite(current)] as const)
          : null;
      } catch {
        // Rewrite recovery is optional. Inbox candidates must remain usable if the
        // referenced run is missing/stale or recovery itself is temporarily unavailable.
        return null;
      }
    }),
  );
  const previews: Record<number, RewritePreview> = {};
  for (const entry of entries) {
    if (entry) previews[entry[0]] = entry[1];
  }
  return previews;
}

export async function applyCurrentStructuredRewrite(
  channelId: number,
  candidateId: number,
  runId: number,
): Promise<ContentDetail> {
  return studioRequest<ContentDetail>(
    `/api/studio/channels/${channelId}/candidates/${candidateId}/rewrite/ai/structured/apply`,
    { method: 'POST', body: JSON.stringify({ run_id: runId }) },
  );
}
