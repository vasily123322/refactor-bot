import { getRawInitData } from './telegram';
import type { ContentCandidateView } from './types';

export type SuggestedPostAction = 'approve' | 'decline';

export class SuggestedPostActionApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

const AUTHORITY_REFRESH_STATUSES = new Set([403, 409, 410]);

export function isSuggestedPostAuthorityError(error: unknown): boolean {
  return error instanceof SuggestedPostActionApiError
    && AUTHORITY_REFRESH_STATUSES.has(error.status);
}

export async function reconcileSuggestedPostActionFailure(
  error: unknown,
  refreshAuthority: () => Promise<unknown>,
): Promise<void> {
  if (!isSuggestedPostAuthorityError(error)) return;
  await refreshAuthority();
}

async function actionRequest<T>(path: string, init: RequestInit): Promise<T> {
  const initData = getRawInitData();
  if (!initData) {
    throw new SuggestedPostActionApiError(
      'Откройте Studio из Telegram, чтобы авторизоваться.',
      401,
    );
  }
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Init-Data': initData,
      ...init.headers,
    },
  });
  if (!response.ok) {
    let detail = response.statusText || 'Suggested Post action failed';
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Keep the product-safe HTTP status text when no JSON detail is available.
    }
    throw new SuggestedPostActionApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export async function submitSuggestedPostAction(
  candidateId: number,
  action: SuggestedPostAction,
  comment: string | null = null,
): Promise<ContentCandidateView> {
  const normalizedComment = action === 'decline' ? (comment || '').trim() : '';
  if (normalizedComment.length > 128) {
    throw new SuggestedPostActionApiError(
      'Комментарий отклонения не может быть длиннее 128 символов.',
      422,
    );
  }
  const body: { action: SuggestedPostAction; comment?: string } = { action };
  if (action === 'decline' && normalizedComment) body.comment = normalizedComment;

  return actionRequest<ContentCandidateView>(
    `/api/studio/candidates/${candidateId}/suggested-post-action`,
    {
      method: 'POST',
      body: JSON.stringify(body),
    },
  );
}
