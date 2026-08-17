import { getRawInitData } from './telegram';
import type { ContentCandidateView, ContentDetail, PostDocument } from './types';

export type RewritePreview = {
  runId: number;
  text: string;
  model: string | null;
  kind: 'text' | 'structured';
  document: PostDocument | null;
};

export type CurrentStructuredRewrite = {
  candidate_id: number;
  run_id: number;
  provider: string;
  model: string | null;
  text: string;
  document: PostDocument;
};

export function rewriteProvenanceLabel(
  preview: RewritePreview,
  candidateId: number,
): string {
  const proposalKind = preview.kind === 'structured' ? 'AI Rich proposal' : 'AI rewrite proposal';
  const authority = preview.kind === 'structured'
    ? 'current at last server check'
    : 'current at generation';
  const model = preview.model ? ` · ${preview.model}` : '';
  return `${proposalKind} · ${authority} · candidate #${candidateId} · run #${preview.runId}${model}`;
}

async function authorityRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const initData = getRawInitData();
  if (!initData) throw new Error('Откройте Studio из Telegram, чтобы авторизоваться.');
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Init-Data': initData,
      ...init.headers,
    },
  });
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
  return (await response.json()) as T;
}

export function previewFromCurrentStructuredRewrite(
  result: CurrentStructuredRewrite,
): RewritePreview {
  return {
    runId: result.run_id,
    text: result.text,
    model: result.model,
    kind: 'structured',
    document: result.document,
  };
}

export async function currentCandidateStructuredRewrite(
  channelId: number,
  candidateId: number,
): Promise<CurrentStructuredRewrite | null> {
  return authorityRequest<CurrentStructuredRewrite | null>(
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
  return authorityRequest<ContentDetail>(
    `/api/studio/channels/${channelId}/candidates/${candidateId}/rewrite/ai/structured/apply`,
    { method: 'POST', body: JSON.stringify({ run_id: runId }) },
  );
}
