import { StudioApiError, type MediaAssetView } from './api';
import { getRawInitData } from './telegram';

export type CandidateMediaView = {
  candidate_id: number;
  source_document_id: number;
  kind: string;
  mime_type: string | null;
  size_bytes: number | null;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  promotable: boolean;
  media_asset_id: number | null;
};

async function authenticatedJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const initData = getRawInitData();
  if (!initData) {
    throw new StudioApiError('Откройте Studio из Telegram, чтобы авторизоваться.', 401);
  }
  const response = await fetch(path, {
    ...init,
    headers: {
      'X-Telegram-Init-Data': initData,
      ...init.headers,
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Keep status text when response is not JSON.
    }
    throw new StudioApiError(detail || 'Studio API error', response.status);
  }
  return (await response.json()) as T;
}

export function loadCandidateMedia(
  channelId: number,
  limit = 100,
): Promise<CandidateMediaView[]> {
  return authenticatedJson<CandidateMediaView[]>(
    `/api/studio/channels/${channelId}/candidate-media?limit=${encodeURIComponent(String(limit))}`,
  );
}

export function promoteCandidateMedia(
  channelId: number,
  candidateId: number,
): Promise<MediaAssetView> {
  return authenticatedJson<MediaAssetView>(
    `/api/studio/channels/${channelId}/candidates/${candidateId}/media-asset`,
    { method: 'POST' },
  );
}

function kindLabel(kind: string): string {
  switch (kind) {
    case 'photo': return 'Фото';
    case 'video': return 'Видео';
    case 'animation': return 'Анимация';
    case 'audio': return 'Аудио';
    case 'voice_note': return 'Voice note';
    case 'video_note': return 'Video note';
    case 'sticker': return 'Стикер';
    case 'document': return 'Файл';
    default: return 'Медиа';
  }
}

function sizeLabel(sizeBytes: number | null): string | null {
  if (sizeBytes === null || !Number.isFinite(sizeBytes) || sizeBytes <= 0) return null;
  if (sizeBytes >= 1024 * 1024) return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${Math.max(1, Math.ceil(sizeBytes / 1024))} KiB`;
}

export function candidateMediaLabel(media: CandidateMediaView): string {
  const parts = [`▣ ${kindLabel(media.kind)}`];
  const size = sizeLabel(media.size_bytes);
  if (size) parts.push(size);
  if (media.duration_seconds && media.duration_seconds > 0) {
    parts.push(`${media.duration_seconds}s`);
  }
  if (media.media_asset_id) {
    parts.push(`Asset #${media.media_asset_id}`);
  }
  return parts.join(' · ');
}
