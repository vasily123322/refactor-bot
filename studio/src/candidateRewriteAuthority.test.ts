import { describe, expect, it } from 'vitest';

import {
  loadCurrentStructuredRewritePreviews,
  previewFromCurrentStructuredRewrite,
  type CurrentStructuredRewrite,
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
  it('preserves the server-returned semantic PostDocument without editing it', () => {
    const preview = previewFromCurrentStructuredRewrite(current);
    expect(preview.kind).toBe('structured');
    expect(preview.runId).toBe(42);
    expect(preview.document).toEqual(document);
    expect(preview.document).toBe(document);
  });

  it('recovers only rewrite candidates and degrades missing/stale runs to no preview', async () => {
    const calls: number[] = [];
    const previews = await loadCurrentStructuredRewritePreviews(
      7,
      [candidate(10), candidate(11), candidate(12, 'reference_only')],
      async (_channelId, candidateId) => {
        calls.push(candidateId);
        if (candidateId === 10) return current;
        throw new Error('referenced run missing');
      },
    );

    expect(calls).toEqual([10, 11]);
    expect(Object.keys(previews)).toEqual(['10']);
    expect(previews[10].document).toEqual(document);
  });
});
