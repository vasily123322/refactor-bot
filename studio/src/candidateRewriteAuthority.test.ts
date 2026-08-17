import { describe, expect, it } from 'vitest';

import {
  isRewriteAuthorityStaleError,
  loadCurrentStructuredRewritePreviews,
  previewFromCurrentStructuredRewrite,
  rewriteProvenanceLabel,
  type CurrentStructuredRewrite,
  type RewritePreview,
} from './candidateRewriteAuthority';
import type { ContentCandidateView, PostDocument } from './types';

const document: PostDocument = {
  schema_version: 1,
  mode: 'rich',
  blocks: [{ id: 'p', type: 'paragraph', content: [{ text: 'Recovered', marks: ['bold'] }] }],
  telegram: {},
  metadata: { ai_generation_kind: 'structured_post_document_v1' },
};

const current: CurrentStructuredRewrite = {
  candidate_id: 10,
  run_id: 42,
  provider: 'channel_ai_structured',
  model: 'model-v1',
  text: 'Recovered',
  document,
};

function candidate(id: number, reusePolicy = 'rewrite_with_attribution'): ContentCandidateView {
  return {
    id,
    source_document_id: id + 100,
    connector_id: 1,
    status: 'new',
    suggested_action: 'rewrite',
    score: null,
    topic: null,
    summary: null,
    source_title: 'Source',
    source_url: null,
    excerpt: 'Source excerpt',
    published_at: null,
    fetched_at: null,
    created_at: null,
    reuse_policy: reusePolicy,
  };
}

describe('current structured rewrite recovery', () => {
  it('preserves the server-returned semantic PostDocument and server-derived run identity', () => {
    const preview = previewFromCurrentStructuredRewrite(current);
    expect(preview.kind).toBe('structured');
    expect(preview.runId).toBe(42);
    expect(preview.provider).toBe('channel_ai_structured');
    expect(preview.provenanceStatus).toBe('current_at_last_server_check');
    expect(preview.document).toEqual(document);
    expect(preview.document).toBe(document);
    expect(rewriteProvenanceLabel(preview, 10, 110)).toContain('run #42');
  });

  it('recovers only server-current rewrite candidates and degrades missing/stale/failed runs to no preview', async () => {
    const calls: number[] = [];
    const previews = await loadCurrentStructuredRewritePreviews(
      7,
      [candidate(10), candidate(11), candidate(12, 'reference_only')],
      async (_channelId, candidateId) => {
        calls.push(candidateId);
        if (candidateId === 10) return current;
        throw new Error('referenced run missing or stale');
      },
    );

    expect(calls).toEqual([10, 11]);
    expect(Object.keys(previews)).toEqual(['10']);
    expect(previews[10].document).toEqual(document);
    expect(previews[10].provenanceStatus).toBe('current_at_last_server_check');
  });
});

describe('structured rewrite provenance presentation', () => {
  const structuredPreview: RewritePreview = {
    runId: 77,
    text: 'Proposal',
    provider: 'channel_ai_structured',
    model: 'model-v2',
    kind: 'structured',
    document,
    provenanceStatus: 'current_at_last_server_check',
  };

  it('labels current proposals conservatively with candidate/source/run/provider/model provenance', () => {
    const label = rewriteProvenanceLabel(structuredPreview, 5, 105);
    expect(label).toBe(
      'AI Rich proposal · current at last server check · candidate #5 · source #105 · run #77 · provider channel_ai_structured · model model-v2',
    );
  });

  it('renders server-rejected visible proposals as stale for both preflight and late-CAS rejection', () => {
    const label = rewriteProvenanceLabel(
      { ...structuredPreview, provenanceStatus: 'stale' },
      5,
      105,
    );
    expect(label).toContain(' · stale · ');
    expect(label).not.toContain('current at last server check');
    expect(isRewriteAuthorityStaleError(new Error('candidate structured rewrite is no longer current'))).toBe(true);
    expect(isRewriteAuthorityStaleError(new Error('candidate rewrite authority changed during rewrite'))).toBe(true);
  });

  it('never presents failed or historical state as current, regardless of run identity', () => {
    const failed = rewriteProvenanceLabel(
      { ...structuredPreview, runId: 9999, provenanceStatus: 'failed' },
      5,
    );
    const historical = rewriteProvenanceLabel(
      { ...structuredPreview, runId: 10000, provenanceStatus: 'historical' },
      5,
    );
    expect(failed).toContain(' · failed · ');
    expect(historical).toContain(' · historical · ');
    expect(failed).not.toContain('current at last server check');
    expect(historical).not.toContain('current at last server check');
  });

  it('degrades missing provider/model to a readable label without inventing identifiers', () => {
    const label = rewriteProvenanceLabel(
      { ...structuredPreview, provider: null, model: null },
      5,
      105,
    );
    expect(label).toBe(
      'AI Rich proposal · current at last server check · candidate #5 · source #105 · run #77',
    );
  });

  it('is pure presentation and does not mutate proposal authority/document state', () => {
    const before = JSON.stringify(structuredPreview);
    rewriteProvenanceLabel(structuredPreview, 5, 105);
    expect(JSON.stringify(structuredPreview)).toBe(before);
  });

  it('returns no provenance block when there is no structured proposal', () => {
    expect(rewriteProvenanceLabel(null, 5, 105)).toBeNull();
    expect(rewriteProvenanceLabel(undefined, 5, 105)).toBeNull();
    expect(rewriteProvenanceLabel(
      { ...structuredPreview, kind: 'text', document: null },
      5,
      105,
    )).toBeNull();
  });
});
