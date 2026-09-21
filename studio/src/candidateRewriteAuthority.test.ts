import { afterEach, describe, expect, it, vi } from 'vitest';

import { StudioApiError } from './api';
import { promptCandidateStructuredRewrite } from './candidatePromptRewrite';
import {
  isRewriteAuthorityStaleError,
  loadCurrentStructuredRewritePreviews,
  previewFromCurrentStructuredRewrite,
  rewriteProvenanceLabel,
  type CurrentStructuredRewrite,
  type RewritePreview,
} from './candidateRewriteAuthority';
import { editCurrentStructuredRewrite } from './candidateStructuredEdit';
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

  it('renders server-rejected visible proposals as stale and classifies conflict by HTTP status', () => {
    const label = rewriteProvenanceLabel(
      { ...structuredPreview, provenanceStatus: 'stale' },
      5,
      105,
    );
    expect(label).toContain(' · stale · ');
    expect(label).not.toContain('current at last server check');
    expect(isRewriteAuthorityStaleError(
      new StudioApiError('localized detail can change', 409),
    )).toBe(true);
    expect(isRewriteAuthorityStaleError(
      new StudioApiError('candidate rewrite authority changed during rewrite', 422),
    )).toBe(false);
    expect(isRewriteAuthorityStaleError(
      new Error('candidate structured rewrite is no longer current'),
    )).toBe(false);
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

function stubTelegramLaunch() {
  vi.stubGlobal('window', {
    location: {
      search: '?tgWebAppData=signed-test-init-data',
      hash: '',
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('structured rewrite typed Studio API errors', () => {
  it('preserves 401 when Telegram init data is missing', async () => {
    vi.stubGlobal('window', {
      location: {
        search: '',
        hash: '',
      },
    });

    try {
      await currentCandidateStructuredRewrite(7, 10);
      throw new Error('expected current rewrite request to fail');
    } catch (error) {
      expect(error).toBeInstanceOf(StudioApiError);
      expect((error as StudioApiError).status).toBe(401);
      expect((error as StudioApiError).message).toContain('Telegram');
    }
  });

  it('preserves 409 detail for structured edit and marks it stale by status', async () => {
    stubTelegramLaunch();
    vi.stubGlobal('fetch', vi.fn(async () => (
      new Response(JSON.stringify({ detail: 'authority changed on server' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      })
    )));

    try {
      await editCurrentStructuredRewrite(7, 10, 42, 'shorten');
      throw new Error('expected structured edit to fail');
    } catch (error) {
      expect(error).toBeInstanceOf(StudioApiError);
      expect((error as StudioApiError).status).toBe(409);
      expect((error as StudioApiError).message).toBe('authority changed on server');
      expect(isRewriteAuthorityStaleError(error)).toBe(true);
    }
  });

  it('preserves 422 detail for prompt rewrite without treating it as stale', async () => {
    stubTelegramLaunch();
    vi.stubGlobal('fetch', vi.fn(async () => (
      new Response(JSON.stringify({ detail: 'instruction is invalid' }), {
        status: 422,
        headers: { 'Content-Type': 'application/json' },
      })
    )));

    try {
      await promptCandidateStructuredRewrite(7, 10, '');
      throw new Error('expected prompt rewrite to fail');
    } catch (error) {
      expect(error).toBeInstanceOf(StudioApiError);
      expect((error as StudioApiError).status).toBe(422);
      expect((error as StudioApiError).message).toBe('instruction is invalid');
      expect(isRewriteAuthorityStaleError(error)).toBe(false);
    }
  });
});

