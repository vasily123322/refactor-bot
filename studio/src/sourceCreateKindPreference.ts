export type SourceCreateKindPreference = 'rss' | 'url' | 'telegram';

type EditorUiPreferences = Readonly<{
  sourceCreateKind?: SourceCreateKindPreference;
  [key: string]: unknown;
}>;

function parseEnvelope(value: string | null | undefined): Record<string, unknown> {
  if (!value) return {};
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
      ? { ...(parsed as Record<string, unknown>) }
      : {};
  } catch {
    return {};
  }
}

export function parseSourceCreateKindPreference(
  value: string | null | undefined,
): SourceCreateKindPreference | null {
  const envelope = parseEnvelope(value) as EditorUiPreferences;
  const kind = envelope.sourceCreateKind;
  return kind === 'rss' || kind === 'url' || kind === 'telegram' ? kind : null;
}

export function serializeSourceCreateKindPreference(
  currentValue: string | null | undefined,
  kind: SourceCreateKindPreference,
): string {
  return JSON.stringify({ ...parseEnvelope(currentValue), sourceCreateKind: kind });
}
