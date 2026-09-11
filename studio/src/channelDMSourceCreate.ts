import type { CreateSourceInput } from './api';
import type { SourceCreateKindPreference } from './sourceCreateKindPreference';

export const CHANNEL_DM_SOURCE_KIND = 'telegram_channel_dms' as const;

export type SourceCreateFormInput = Readonly<{
  kind: SourceCreateKindPreference;
  value: string;
  channelTgChatId: number | string;
  mode: CreateSourceInput['mode'];
  citationEnabled: boolean;
  reusePolicy: CreateSourceInput['reuse_policy'];
}>;

export function sourceCreateNeedsValue(kind: SourceCreateKindPreference): boolean {
  return kind !== CHANNEL_DM_SOURCE_KIND;
}

export function buildSourceCreateInput(input: SourceCreateFormInput): CreateSourceInput | null {
  const value = sourceCreateNeedsValue(input.kind)
    ? input.value.trim()
    : String(input.channelTgChatId).trim();
  if (!value) return null;

  return {
    kind: input.kind,
    value,
    mode: input.mode,
    citation_enabled: input.citationEnabled,
    reuse_policy: input.reusePolicy,
  };
}

export function hasChannelDMSource(kinds: readonly string[]): boolean {
  return kinds.some((kind) => kind === CHANNEL_DM_SOURCE_KIND);
}
