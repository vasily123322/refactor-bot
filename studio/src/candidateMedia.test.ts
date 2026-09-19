import { describe, expect, it } from 'vitest';

import {
  candidateMediaBatchPath,
  candidateMediaLabel,
  mergeCandidateMediaMap,
  type CandidateMediaView,
} from './candidateMedia';

function media(patch: Partial<CandidateMediaView> = {}): CandidateMediaView {
  return {
    candidate_id: 1,
    source_document_id: 2,
    kind: 'photo',
    mime_type: 'image/jpeg',
    size_bytes: 345678,
    width: 1600,
    height: 900,
    duration_seconds: null,
    promotable: true,
    media_asset_id: null,
    ...patch,
  };
}

describe('candidate media labels', () => {
  it('renders a compact photo size label', () => {
    expect(candidateMediaLabel(media())).toBe('▣ Фото · 338 KiB');
  });

  it('renders duration without exposing transport details', () => {
    const label = candidateMediaLabel(media({ kind: 'video', size_bytes: 2_621_440, duration_seconds: 18 }));
    expect(label).toBe('▣ Видео · 2.5 MiB · 18s');
    expect(label).not.toContain('file_id');
    expect(label).not.toContain('access_hash');
  });

  it('shows only durable MediaAsset identity after promotion', () => {
    const label = candidateMediaLabel(media({ media_asset_id: 77 }));
    expect(label).toBe('▣ Фото · 338 KiB · Asset #77');
    expect(label).not.toContain('telegram');
  });
});


describe('candidate media page batches', () => {
  it('builds an exact de-duplicated candidate id request', () => {
    expect(candidateMediaBatchPath(7, [12, 9, 12])).toBe(
      '/api/studio/channels/7/candidate-media?candidate_ids=12%2C9',
    );
  });

  it('merges older-page media by candidate id without dropping loaded rows', () => {
    const current = {
      1: media({ candidate_id: 1, media_asset_id: 10 }),
    };
    const merged = mergeCandidateMediaMap(current, [
      media({ candidate_id: 2, source_document_id: 20 }),
    ]);

    expect(Object.keys(merged).map(Number).sort((a, b) => a - b)).toEqual([1, 2]);
    expect(merged[1]?.media_asset_id).toBe(10);
    expect(merged[2]?.source_document_id).toBe(20);
  });
});
