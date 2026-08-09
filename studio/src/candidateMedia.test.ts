import { describe, expect, it } from 'vitest';

import { candidateMediaLabel, type CandidateMediaView } from './candidateMedia';

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
});
