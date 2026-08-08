import { useEffect, useMemo, useState } from 'react';

import type { SourceSettingsPatch } from './sourceSettingsApi';
import type { SourceConnectorView } from './types';

const REUSE_POLICIES = [
  ['reference_only', 'Только reference'],
  ['summarize', 'Можно summarization'],
  ['quote_with_attribution', 'Цитаты + attribution'],
  ['rewrite_with_attribution', 'Rewrite + attribution'],
  ['mirror_authorized', 'Mirror разрешён'],
] as const;

export function SourceSettingsControls({
  source,
  busy,
  onSave,
}: {
  source: SourceConnectorView;
  busy: boolean;
  onSave: (patch: SourceSettingsPatch) => Promise<void>;
}) {
  const [enabled, setEnabled] = useState(source.enabled);
  const [mode, setMode] = useState<'summary' | 'rewrite'>(
    source.mode === 'rewrite' ? 'rewrite' : 'summary',
  );
  const [citationEnabled, setCitationEnabled] = useState(source.citation_enabled);
  const [reusePolicy, setReusePolicy] = useState<SourceSettingsPatch['reuse_policy']>(
    REUSE_POLICIES.some(([value]) => value === source.reuse_policy)
      ? (source.reuse_policy as SourceSettingsPatch['reuse_policy'])
      : 'reference_only',
  );

  useEffect(() => {
    setEnabled(source.enabled);
    setMode(source.mode === 'rewrite' ? 'rewrite' : 'summary');
    setCitationEnabled(source.citation_enabled);
    setReusePolicy(
      REUSE_POLICIES.some(([value]) => value === source.reuse_policy)
        ? (source.reuse_policy as SourceSettingsPatch['reuse_policy'])
        : 'reference_only',
    );
  }, [source]);

  const dirty = useMemo(
    () =>
      enabled !== source.enabled ||
      mode !== source.mode ||
      citationEnabled !== source.citation_enabled ||
      reusePolicy !== source.reuse_policy,
    [citationEnabled, enabled, mode, reusePolicy, source],
  );

  return (
    <div className="source-settings-controls">
      <label className="source-settings-toggle">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        <span>{enabled ? 'Источник включён' : 'Источник выключен'}</span>
      </label>
      <label>
        <span>Mode</span>
        <select value={mode} onChange={(event) => setMode(event.target.value as 'summary' | 'rewrite')}>
          <option value="summary">Summary</option>
          <option value="rewrite">Rewrite</option>
        </select>
      </label>
      <label>
        <span>Reuse policy</span>
        <select
          value={reusePolicy}
          onChange={(event) =>
            setReusePolicy(event.target.value as SourceSettingsPatch['reuse_policy'])
          }
        >
          {REUSE_POLICIES.map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </label>
      <label className="source-settings-toggle">
        <input
          type="checkbox"
          checked={citationEnabled}
          onChange={(event) => setCitationEnabled(event.target.checked)}
        />
        <span>Citation metadata</span>
      </label>
      <button
        className="button primary compact"
        disabled={busy || !dirty}
        onClick={() =>
          void onSave({
            enabled,
            mode,
            citation_enabled: citationEnabled,
            reuse_policy: reusePolicy,
          })
        }
      >
        {busy ? 'Сохраняю…' : dirty ? 'Сохранить' : 'Сохранено'}
      </button>
    </div>
  );
}
