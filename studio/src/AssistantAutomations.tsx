import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import type {
  AssistantAutomationCreateInput,
  AssistantAutomationRunSummaryView,
  AssistantAutomationView,
  AssistantSkillView,
} from './api';
import { AsyncRegion, SkeletonBlock } from './AsyncUI';
import {
  AUTOMATION_HISTORY_PAGE_SIZE,
  automationHistoryCursor,
  mergeAutomationHistoryPage,
  splitAutomationHistoryPage,
} from './automationRunHistory';
import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveChannelDataView,
  runExclusiveOperation,
  type ChannelLoadState,
  type ChannelRequestToken,
  type ScopedLoadState,
} from './asyncControl';
import type { Channel } from './types';


function automationErrorMessage(error: unknown): string {
  if (error instanceof StudioApiError || error instanceof Error) return error.message;
  return 'Неизвестная ошибка';
}

function automationRequestId(): string {
  const randomUUID = globalThis.crypto?.randomUUID?.bind(globalThis.crypto);
  if (randomUUID) return `automation-${randomUUID()}`;
  return `automation-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

function nextRunLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('ru', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

export function eligibleAutomationSkills(
  skills: AssistantSkillView[],
): AssistantSkillView[] {
  return skills.filter((skill) => skill.automation_allowed === true);
}

export function mergeAssistantAutomation(
  previous: AssistantAutomationView[],
  automation: AssistantAutomationView,
): AssistantAutomationView[] {
  return [
    automation,
    ...previous.filter((item) => item.id !== automation.id),
  ].sort((left, right) => right.id - left.id);
}

export function automationCadenceLabel(
  automation: Pick<AssistantAutomationView, 'cadence'>,
): string {
  if (automation.cadence.kind === 'weekly') {
    const weekday = automation.cadence.weekday ?? 0;
    const labels = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
    return `weekly · ${labels[weekday] ?? weekday} · ${automation.cadence.local_time}`;
  }
  return `daily · ${automation.cadence.local_time}`;
}

export function automationScheduleSummary(
  automation: Pick<AssistantAutomationView, 'cadence' | 'timezone' | 'next_run_at'>,
): string {
  return `${automationCadenceLabel(automation)} · ${automation.timezone} · Следующий запуск: ${nextRunLabel(automation.next_run_at)}`;
}

export function automationHealthLabel(
  automation: Pick<AssistantAutomationView, 'health' | 'health_reason'>,
): string {
  const labels: Record<AssistantAutomationView['health'], string> = {
    active: 'Активна',
    paused: 'Приостановлена',
    blocked: 'Заблокирована',
    needs_attention: 'Требует внимания',
  };
  return `${labels[automation.health]} · ${automation.health_reason}`;
}

function automationRunSummary(run: AssistantAutomationRunSummaryView): string {
  const phase = run.workflow_phase ? ` · ${run.workflow_phase}` : '';
  const model = run.model ? ` · ${run.model}` : '';
  return `#${run.id} · ${run.status}${phase} · ${run.tokens_used} tokens${model}`;
}

export type AutomationControl =
  | 'pause'
  | 'enable'
  | 'resume'
  | 'history'
  | 'replacement';

export function automationAvailableControls(
  automation: AssistantAutomationView,
): AutomationControl[] {
  const controls: AutomationControl[] = [];
  if (automation.enabled) controls.push('pause');
  if (
    !automation.enabled
    && automation.health === 'paused'
    && automation.disabled_reason === 'manual_pause'
  ) controls.push('enable');
  if (automation.latest_run?.resumable) controls.push('resume');
  controls.push('history');
  if (
    automation.migration_available
    && automation.suggested_skill_id
    && automation.suggested_skill_version
  ) controls.push('replacement');
  return controls;
}

export function automationDataView(
  state: ChannelLoadState | null,
  channelId: number,
  count: number,
) {
  return resolveChannelDataView(state, channelId, count);
}

export function validateAutomationForm({
  skill,
  cadenceKind,
  localTime,
  weekday,
  seriesBrief,
  seriesPostCount,
}: {
  skill: AssistantSkillView | null;
  cadenceKind: 'daily' | 'weekly';
  localTime: string;
  weekday: number;
  seriesBrief: string;
  seriesPostCount: number;
}): string | null {
  if (!skill || skill.automation_allowed !== true) return 'Выберите доступный skill.';
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(localTime)) {
    return 'Укажите локальное время в формате HH:MM.';
  }
  if (cadenceKind === 'weekly' && (!Number.isInteger(weekday) || weekday < 0 || weekday > 6)) {
    return 'Для weekly выберите weekday 0..6.';
  }
  if (skill.scenario === 'prepare_content_series') {
    const brief = seriesBrief.trim();
    if (brief.length < 20 || brief.length > 2000) {
      return 'Brief серии должен содержать 20–2000 символов.';
    }
    if (!Number.isInteger(seriesPostCount) || seriesPostCount < 2 || seriesPostCount > 8) {
      return 'Количество постов должно быть 2–8.';
    }
  }
  return null;
}

function buildCreateInput({
  skill,
  requestId,
  cadenceKind,
  localTime,
  weekday,
  seriesBrief,
  seriesPostCount,
}: {
  skill: AssistantSkillView;
  requestId: string;
  cadenceKind: 'daily' | 'weekly';
  localTime: string;
  weekday: number;
  seriesBrief: string;
  seriesPostCount: number;
}): AssistantAutomationCreateInput {
  const operatorInput = skill.scenario === 'prepare_content_series'
    ? {
      brief: seriesBrief.trim(),
      post_count: seriesPostCount,
    }
    : {};
  return {
    request_id: requestId,
    skill_id: skill.skill_id,
    skill_version: skill.version,
    operator_input: operatorInput,
    cadence: {
      kind: cadenceKind,
      local_time: localTime,
      ...(cadenceKind === 'weekly' ? { weekday } : {}),
    },
  };
}

export function AssistantAutomations({
  channel,
  skills,
}: {
  channel: Channel;
  skills: AssistantSkillView[];
}) {
  const eligibleSkills = useMemo(() => eligibleAutomationSkills(skills), [skills]);
  const [automations, setAutomations] = useState<AssistantAutomationView[]>([]);
  const [loadState, setLoadState] = useState<ChannelLoadState | null>(null);
  const [error, setError] = useState<{ channelId: number; message: string } | null>(null);
  const [selectedSkillKey, setSelectedSkillKey] = useState('');
  const [cadenceKind, setCadenceKind] = useState<'daily' | 'weekly'>('daily');
  const [localTime, setLocalTime] = useState('09:00');
  const [weekday, setWeekday] = useState(0);
  const [seriesBrief, setSeriesBrief] = useState('');
  const [seriesPostCount, setSeriesPostCount] = useState(3);
  const [creating, setCreating] = useState(false);
  const [toggleBusyIds, setToggleBusyIds] = useState<Set<string>>(() => new Set());
  const [resumeBusyIds, setResumeBusyIds] = useState<Set<string>>(() => new Set());
  const [replacementBusyIds, setReplacementBusyIds] = useState<Set<string>>(() => new Set());
  const [historyAutomationId, setHistoryAutomationId] = useState<number | null>(null);
  const [historyRows, setHistoryRows] = useState<AssistantAutomationRunSummaryView[]>([]);
  const [historyState, setHistoryState] = useState<ScopedLoadState | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const [historyLoadMoreError, setHistoryLoadMoreError] = useState<string | null>(null);

  const channelIdRef = useRef(channel.id);
  const loadOwnershipRef = useRef(new ChannelRequestOwnership());
  const createOwnershipRef = useRef(new ChannelRequestOwnership());
  const operationContextOwnershipRef = useRef(new ChannelRequestOwnership());
  const operationContextRef = useRef<{
    channelId: number | null;
    token: ChannelRequestToken | null;
  }>({ channelId: null, token: null });
  const createLockRef = useRef(new ExclusiveOperationLock());
  const automationLocksRef = useRef(new Map<string, ExclusiveOperationLock>());
  const historyOwnershipRef = useRef(new ChannelRequestOwnership());
  const historyScopeRef = useRef<string | null>(null);
  const replacementRequestIdsRef = useRef(new Map<string, string>());
  const validDataChannelRef = useRef<number | null>(null);
  const pendingCreateRef = useRef<{
    channelId: number;
    fingerprint: string;
    requestId: string;
  } | null>(null);
  if (operationContextRef.current.channelId !== channel.id) {
    operationContextRef.current.channelId = channel.id;
    operationContextRef.current.token = operationContextOwnershipRef.current.begin(channel.id);
  }
  channelIdRef.current = channel.id;

  const selectedSkill = eligibleSkills.find(
    (skill) => `${skill.skill_id}@${skill.version}` === selectedSkillKey,
  ) ?? eligibleSkills[0] ?? null;

  useEffect(() => {
    if (
      selectedSkill
      && selectedSkillKey === `${selectedSkill.skill_id}@${selectedSkill.version}`
    ) return;
    setSelectedSkillKey(
      eligibleSkills[0]
        ? `${eligibleSkills[0].skill_id}@${eligibleSkills[0].version}`
        : '',
    );
  }, [eligibleSkills, selectedSkill, selectedSkillKey]);

  const loadAutomations = useCallback(async () => {
    const channelId = channelIdRef.current;
    const token = loadOwnershipRef.current.begin(channelId);
    const isCurrent = () => loadOwnershipRef.current.isCurrent(
      token,
      channelIdRef.current,
    );
    const hasValidData = validDataChannelRef.current === channelId;
    if (!hasValidData) {
      validDataChannelRef.current = null;
      setAutomations([]);
      setLoadState({ channelId, phase: 'loading' });
    }
    setError(null);
    try {
      const rows = await studioApi.assistantAutomations(channelId);
      if (!isCurrent()) return;
      validDataChannelRef.current = channelId;
      setAutomations(rows);
      setLoadState({ channelId, phase: 'loaded' });
    } catch (reason) {
      if (!isCurrent()) return;
      if (validDataChannelRef.current !== channelId) {
        setAutomations([]);
        setLoadState({ channelId, phase: 'error-without-valid-data' });
      }
      setError({ channelId, message: automationErrorMessage(reason) });
    }
  }, [channel.id]);

  useEffect(() => {
    loadOwnershipRef.current.invalidate();
    createOwnershipRef.current.invalidate();
    pendingCreateRef.current = null;
    setCreating(false);
    historyOwnershipRef.current.invalidate();
    historyScopeRef.current = null;
    setHistoryAutomationId(null);
    setHistoryRows([]);
    setHistoryState(null);
    setHistoryError(null);
    setHistoryHasMore(false);
    setHistoryLoadingMore(false);
    setHistoryLoadMoreError(null);
    setSelectedSkillKey('');
    setSeriesBrief('');
    setSeriesPostCount(3);
    void loadAutomations();
  }, [channel.id, loadAutomations]);

  const createAutomation = useCallback(async () => {
    const channelId = channelIdRef.current;
    const skill = selectedSkill;
    const validation = validateAutomationForm({
      skill,
      cadenceKind,
      localTime,
      weekday,
      seriesBrief,
      seriesPostCount,
    });
    if (validation || !skill) {
      setError({ channelId, message: validation || 'Automation form is invalid.' });
      return;
    }

    const normalizedDefinition = JSON.stringify({
      skill_id: skill.skill_id,
      skill_version: skill.version,
      operator_input: skill.scenario === 'prepare_content_series'
        ? { brief: seriesBrief.trim(), post_count: seriesPostCount }
        : {},
      cadence: {
        kind: cadenceKind,
        local_time: localTime,
        weekday: cadenceKind === 'weekly' ? weekday : null,
      },
    });
    const pending = pendingCreateRef.current;
    const requestId = (
      pending
      && pending.channelId === channelId
      && pending.fingerprint === normalizedDefinition
    )
      ? pending.requestId
      : automationRequestId();
    pendingCreateRef.current = {
      channelId,
      fingerprint: normalizedDefinition,
      requestId,
    };

    const result = await runExclusiveOperation(
      createLockRef.current,
      `create:${channelId}`,
      async () => {
        const token = createOwnershipRef.current.begin(channelId);
        const isCurrent = () => createOwnershipRef.current.isCurrent(
          token,
          channelIdRef.current,
        );
        setCreating(true);
        setError(null);
        try {
          const created = await studioApi.createAssistantAutomation(
            channelId,
            buildCreateInput({
              skill,
              requestId,
              cadenceKind,
              localTime,
              weekday,
              seriesBrief,
              seriesPostCount,
            }),
          );
          if (!isCurrent()) return;
          setAutomations((previous) => mergeAssistantAutomation(previous, created));
          validDataChannelRef.current = channelId;
          setLoadState({ channelId, phase: 'loaded' });
          pendingCreateRef.current = null;
        } catch (reason) {
          if (isCurrent()) {
            setError({ channelId, message: automationErrorMessage(reason) });
          }
        } finally {
          if (isCurrent()) setCreating(false);
        }
      },
    );
    if (!result.started) return;
  }, [
    selectedSkill,
    cadenceKind,
    localTime,
    weekday,
    seriesBrief,
    seriesPostCount,
  ]);

  const toggleEnabled = useCallback(async (
    automation: AssistantAutomationView,
  ) => {
    const channelId = channelIdRef.current;
    const operationContextToken = operationContextRef.current.token;
    if (operationContextToken === null) return;
    const isCurrent = () => operationContextOwnershipRef.current.isCurrent(
      operationContextToken,
      channelIdRef.current,
    );
    const resourceKey = `${channelId}:${automation.id}`;
    let lock = automationLocksRef.current.get(resourceKey);
    if (!lock) {
      lock = new ExclusiveOperationLock();
      automationLocksRef.current.set(resourceKey, lock);
    }
    const result = await runExclusiveOperation(
      lock,
      `toggle:${automation.id}`,
      async () => {
        setToggleBusyIds((previous) => new Set(previous).add(resourceKey));
        if (isCurrent()) setError(null);
        try {
          const updated = await studioApi.setAssistantAutomationEnabled(
            channelId,
            automation.id,
            !automation.enabled,
          );
          if (!isCurrent()) return;
          setAutomations((previous) => mergeAssistantAutomation(previous, updated));
        } catch (reason) {
          if (isCurrent()) {
            setError({ channelId, message: automationErrorMessage(reason) });
          }
        } finally {
          setToggleBusyIds((previous) => {
            const next = new Set(previous);
            next.delete(resourceKey);
            return next;
          });
        }
      },
    );
    if (!result.started) return;
  }, []);

  const loadHistory = useCallback(async (automationId: number) => {
    const channelId = channelIdRef.current;
    const scopeKey = String(automationId);
    historyScopeRef.current = scopeKey;
    setHistoryAutomationId(automationId);
    setHistoryRows([]);
    setHistoryError(null);
    setHistoryHasMore(false);
    setHistoryLoadingMore(false);
    setHistoryLoadMoreError(null);
    setHistoryState({ channelId, scopeKey, phase: 'loading' });
    const token = historyOwnershipRef.current.begin(channelId, scopeKey);
    try {
      const rows = await studioApi.assistantAutomationRuns(channelId, automationId, {
        limit: AUTOMATION_HISTORY_PAGE_SIZE + 1,
      });
      if (!historyOwnershipRef.current.isCurrent(
        token,
        channelIdRef.current,
        historyScopeRef.current,
      )) return;
      const page = splitAutomationHistoryPage(rows);
      setHistoryRows(page.items);
      setHistoryHasMore(page.hasMore);
      setHistoryState({ channelId, scopeKey, phase: 'loaded' });
    } catch (reason) {
      if (!historyOwnershipRef.current.isCurrent(
        token,
        channelIdRef.current,
        historyScopeRef.current,
      )) return;
      setHistoryRows([]);
      setHistoryHasMore(false);
      setHistoryState({ channelId, scopeKey, phase: 'error-without-valid-data' });
      setHistoryError(automationErrorMessage(reason));
    }
  }, []);

  const loadOlderHistory = useCallback(async (automationId: number) => {
    const channelId = channelIdRef.current;
    const scopeKey = String(automationId);
    if (
      historyScopeRef.current !== scopeKey
      || historyLoadingMore
      || !historyHasMore
    ) return;

    const cursor = automationHistoryCursor(historyRows);
    if (!cursor) {
      setHistoryHasMore(false);
      return;
    }

    const token = historyOwnershipRef.current.begin(channelId, scopeKey);
    setHistoryLoadingMore(true);
    setHistoryLoadMoreError(null);
    try {
      const rows = await studioApi.assistantAutomationRuns(channelId, automationId, {
        limit: AUTOMATION_HISTORY_PAGE_SIZE + 1,
        beforeScheduledFor: cursor.scheduledFor,
        beforeId: cursor.id,
      });
      if (!historyOwnershipRef.current.isCurrent(
        token,
        channelIdRef.current,
        historyScopeRef.current,
      )) return;
      const page = splitAutomationHistoryPage(rows);
      setHistoryRows((current) => mergeAutomationHistoryPage(current, page.items));
      setHistoryHasMore(page.hasMore);
    } catch (reason) {
      if (historyOwnershipRef.current.isCurrent(
        token,
        channelIdRef.current,
        historyScopeRef.current,
      )) {
        setHistoryLoadMoreError(automationErrorMessage(reason));
      }
    } finally {
      if (historyOwnershipRef.current.isCurrent(
        token,
        channelIdRef.current,
        historyScopeRef.current,
      )) {
        setHistoryLoadingMore(false);
      }
    }
  }, [historyHasMore, historyLoadingMore, historyRows]);

  const resumeLatest = useCallback(async (automation: AssistantAutomationView) => {
    const run = automation.latest_run;
    if (!run?.resumable) return;
    const channelId = channelIdRef.current;
    const operationContextToken = operationContextRef.current.token;
    if (operationContextToken === null) return;
    const isCurrent = () => operationContextOwnershipRef.current.isCurrent(
      operationContextToken,
      channelIdRef.current,
    );
    const resourceKey = `${channelId}:${automation.id}`;
    let lock = automationLocksRef.current.get(resourceKey);
    if (!lock) {
      lock = new ExclusiveOperationLock();
      automationLocksRef.current.set(resourceKey, lock);
    }
    await runExclusiveOperation(lock, `resume:${automation.id}`, async () => {
      setResumeBusyIds((previous) => new Set(previous).add(resourceKey));
      if (isCurrent()) setError(null);
      try {
        await studioApi.resumeAssistantRun(channelId, run.id);
        if (!isCurrent()) return;
        await loadAutomations();
        if (!isCurrent()) return;
        await loadHistory(automation.id);
      } catch (reason) {
        if (isCurrent()) {
          setError({ channelId, message: automationErrorMessage(reason) });
        }
      } finally {
        setResumeBusyIds((previous) => {
          const next = new Set(previous);
          next.delete(resourceKey);
          return next;
        });
      }
    });
  }, [loadAutomations, loadHistory]);

  const createReplacement = useCallback(async (automation: AssistantAutomationView) => {
    if (
      !automation.migration_available
      || !automation.suggested_skill_id
      || !automation.suggested_skill_version
    ) return;
    const channelId = channelIdRef.current;
    const operationContextToken = operationContextRef.current.token;
    if (operationContextToken === null) return;
    const isCurrent = () => operationContextOwnershipRef.current.isCurrent(
      operationContextToken,
      channelIdRef.current,
    );
    const resourceKey = `${channelId}:${automation.id}`;
    const existingRequestId = replacementRequestIdsRef.current.get(resourceKey);
    const requestId = existingRequestId ?? automationRequestId();
    replacementRequestIdsRef.current.set(resourceKey, requestId);
    let lock = automationLocksRef.current.get(resourceKey);
    if (!lock) {
      lock = new ExclusiveOperationLock();
      automationLocksRef.current.set(resourceKey, lock);
    }
    await runExclusiveOperation(
      lock,
      `replacement:${automation.id}`,
      async () => {
        setReplacementBusyIds((previous) => new Set(previous).add(resourceKey));
        if (isCurrent()) setError(null);
        try {
          const created = await studioApi.createAssistantAutomation(channelId, {
            request_id: requestId,
            skill_id: automation.suggested_skill_id!,
            skill_version: automation.suggested_skill_version!,
            operator_input: automation.operator_input,
            cadence: {
              kind: automation.cadence.kind,
              local_time: automation.cadence.local_time,
              ...(automation.cadence.kind === 'weekly' && automation.cadence.weekday !== null
                ? { weekday: automation.cadence.weekday }
                : {}),
            },
          });
          if (!isCurrent()) return;
          replacementRequestIdsRef.current.delete(resourceKey);
          setAutomations((previous) => mergeAssistantAutomation(previous, created));
        } catch (reason) {
          if (isCurrent()) {
            setError({ channelId, message: automationErrorMessage(reason) });
          }
        } finally {
          setReplacementBusyIds((previous) => {
            const next = new Set(previous);
            next.delete(resourceKey);
            return next;
          });
        }
      },
    );
  }, []);

  const loadView = automationDataView(loadState, channel.id, automations.length);
  const loading = loadView === 'loading';
  const failed = loadView === 'error-without-valid-data';
  const empty = loadView === 'loaded-empty';
  const formError = validateAutomationForm({
    skill: selectedSkill,
    cadenceKind,
    localTime,
    weekday,
    seriesBrief,
    seriesPostCount,
  });

  return (
    <section className="assistant-automation-card" aria-label="Automations">
      <div className="panel-heading">
        <div>
          <h2>Automations</h2>
          <small>
            Exact skill version · daily/weekly cadence · timezone фиксируется backend.
          </small>
        </div>
      </div>

      {error?.channelId === channel.id && (
        <div className="banner error" role="alert">
          {error.message}
          <button aria-label="Закрыть ошибку" onClick={() => setError(null)}>×</button>
        </div>
      )}

      {eligibleSkills.length > 0 ? (
        <div className="assistant-automation-form">
          <label htmlFor="assistant-automation-skill">Skill</label>
          <select
            id="assistant-automation-skill"
            value={selectedSkill ? `${selectedSkill.skill_id}@${selectedSkill.version}` : ''}
            onChange={(event) => setSelectedSkillKey(event.target.value)}
            disabled={creating}
          >
            {eligibleSkills.map((skill) => (
              <option
                value={`${skill.skill_id}@${skill.version}`}
                key={`${skill.skill_id}@${skill.version}`}
              >
                {skill.display_title} · {skill.skill_id}@{skill.version}
              </option>
            ))}
          </select>

          {selectedSkill?.scenario === 'prepare_content_series' && (
            <div className="assistant-automation-series-input">
              <label htmlFor="assistant-automation-series-brief">Brief серии</label>
              <textarea
                id="assistant-automation-series-brief"
                value={seriesBrief}
                minLength={20}
                maxLength={2000}
                rows={4}
                disabled={creating}
                onChange={(event) => setSeriesBrief(event.target.value)}
              />
              <label htmlFor="assistant-automation-series-count">Количество постов</label>
              <select
                id="assistant-automation-series-count"
                value={seriesPostCount}
                disabled={creating}
                onChange={(event) => setSeriesPostCount(Number(event.target.value))}
              >
                {[2, 3, 4, 5, 6, 7, 8].map((count) => (
                  <option value={count} key={count}>{count}</option>
                ))}
              </select>
            </div>
          )}

          <div className="assistant-automation-cadence">
            <label htmlFor="assistant-automation-cadence">Cadence</label>
            <select
              id="assistant-automation-cadence"
              value={cadenceKind}
              disabled={creating}
              onChange={(event) => setCadenceKind(
                event.target.value === 'weekly' ? 'weekly' : 'daily',
              )}
            >
              <option value="daily">daily</option>
              <option value="weekly">weekly</option>
            </select>
            <label htmlFor="assistant-automation-time">Локальное время</label>
            <input
              id="assistant-automation-time"
              type="time"
              value={localTime}
              disabled={creating}
              required
              onChange={(event) => setLocalTime(event.target.value)}
            />
            {cadenceKind === 'weekly' && (
              <>
                <label htmlFor="assistant-automation-weekday">День недели</label>
                <select
                  id="assistant-automation-weekday"
                  value={weekday}
                  disabled={creating}
                  onChange={(event) => setWeekday(Number(event.target.value))}
                >
                  {['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].map((label, index) => (
                    <option value={index} key={label}>{label}</option>
                  ))}
                </select>
              </>
            )}
          </div>

          <div className="assistant-automation-create-row">
            <small>
              Создание recurring definition не публикует и не ставит Content в расписание.
            </small>
            <button
              className="button secondary"
              disabled={creating || Boolean(formError)}
              onClick={() => void createAutomation()}
            >
              {creating ? 'Создаю automation…' : 'Создать automation'}
            </button>
          </div>
        </div>
      ) : (
        <div className="assistant-empty">
          Backend catalog пока не разрешает ни один skill для automations.
        </div>
      )}

      <AsyncRegion
        className="assistant-automation-list"
        loading={loading}
        error={failed}
        empty={empty}
        loadingLabel="Загружаю automations…"
        loadingFallback={
          <>
            <SkeletonBlock height={54} />
            <SkeletonBlock height={54} />
          </>
        }
        errorFallback={
          <div className="assistant-empty">Automations не загружены.</div>
        }
        emptyFallback={
          <div className="assistant-empty">Recurring automations пока не созданы.</div>
        }
      >
        {automations.map((automation) => {
          const historySelected = historyAutomationId === automation.id;
          const historyScopeKey = String(automation.id);
          const historyLoading = historySelected
            && historyState?.channelId === channel.id
            && historyState.scopeKey === historyScopeKey
            && historyState.phase === 'loading';
          const historyFailed = historySelected
            && historyState?.channelId === channel.id
            && historyState.scopeKey === historyScopeKey
            && historyState.phase === 'error-without-valid-data';
          const controls = automationAvailableControls(automation);
          const canEnable = controls.includes('enable');
          const canPause = controls.includes('pause');
          const automationResourceKey = `${channel.id}:${automation.id}`;
          const automationBusy = toggleBusyIds.has(automationResourceKey)
            || resumeBusyIds.has(automationResourceKey)
            || replacementBusyIds.has(automationResourceKey);
          return (
            <article className="assistant-automation-row assistant-automation-row-e5" key={automation.id}>
              <div className="assistant-automation-main">
                <div className="assistant-automation-title-row">
                  <strong>{automation.skill_id}@{automation.skill_version}</strong>
                  <span
                    className={`assistant-automation-health assistant-automation-health-${automation.health}`}
                    aria-label={`Состояние automation: ${automationHealthLabel(automation)}`}
                  >
                    {automationHealthLabel(automation)}
                  </span>
                </div>
                <small>{automationScheduleSummary(automation)}</small>
                <small>
                  Последний outcome: {automation.last_outcome ?? 'нет'} · 7 дней: {automation.usage_7d.tokens_used} tokens
                  {' · '}30 дней: {automation.usage_30d.tokens_used} tokens
                </small>
                {automation.latest_run && (
                  <small>
                    Последний запуск: {automationRunSummary(automation.latest_run)}
                    {automation.latest_run.resume_state ? ` · ${automation.latest_run.resume_state}` : ''}
                  </small>
                )}
                <small>
                  Envelope: {automation.cadence_occurrences_per_week}/нед.
                  {automation.execution_limits
                    ? ` · max_llm_calls=${automation.execution_limits.max_llm_calls} · max_seconds=${automation.execution_limits.max_seconds}`
                    : ' · pinned version unsupported'}
                  {automation.post_count ? ` · post_count=${automation.post_count}` : ''}
                  {automation.claim_active ? ' · claim active' : ''}
                </small>
                <div className="assistant-automation-actions">
                  {(canPause || canEnable) && (
                    <button
                      className="button secondary"
                      disabled={automationBusy}
                      onClick={() => void toggleEnabled(automation)}
                    >
                      {toggleBusyIds.has(automationResourceKey)
                        ? 'Сохраняю…'
                        : canPause ? 'Приостановить' : 'Включить'}
                    </button>
                  )}
                  {controls.includes('resume') && automation.latest_run?.resumable && (
                    <button
                      className="button secondary"
                      disabled={automationBusy}
                      onClick={() => void resumeLatest(automation)}
                    >
                      {resumeBusyIds.has(automationResourceKey) ? 'Продолжаю…' : 'Продолжить'}
                    </button>
                  )}
                  <button
                    className="button secondary"
                    onClick={() => {
                      if (historySelected) {
                        historyOwnershipRef.current.invalidate();
                        historyScopeRef.current = null;
                        setHistoryAutomationId(null);
                        setHistoryRows([]);
                        setHistoryState(null);
                        setHistoryError(null);
                        setHistoryHasMore(false);
                        setHistoryLoadingMore(false);
                        setHistoryLoadMoreError(null);
                      } else {
                        void loadHistory(automation.id);
                      }
                    }}
                  >
                    {historySelected ? 'Скрыть историю' : 'История запусков'}
                  </button>
                  {controls.includes('replacement')
                    && automation.suggested_skill_id
                    && automation.suggested_skill_version && (
                    <button
                      className="button secondary"
                      disabled={automationBusy}
                      onClick={() => void createReplacement(automation)}
                    >
                      {replacementBusyIds.has(automationResourceKey)
                        ? 'Создаю замену…'
                        : `Создать замену на ${automation.suggested_skill_id}@${automation.suggested_skill_version}`}
                    </button>
                  )}
                </div>
                {historySelected && (
                  <div className="assistant-automation-history" aria-live="polite">
                    {historyLoading && <small>Загружаю историю…</small>}
                    {historyFailed && (
                      <small role="alert">{historyError ?? 'История запусков не загружена.'}</small>
                    )}
                    {!historyLoading && !historyFailed && historyRows.length === 0 && (
                      <small>Запусков ещё нет.</small>
                    )}
                    {!historyLoading && !historyFailed && historyRows.map((run) => (
                      <div className="assistant-automation-history-row" key={run.id}>
                        <small>
                          {run.scheduled_for ? nextRunLabel(run.scheduled_for) : 'без scheduled_for'}
                          {' · '}{automationRunSummary(run)}
                          {' · '}{run.resume_state}
                        </small>
                      </div>
                    ))}
                    {!historyLoading && !historyFailed && historyLoadMoreError && (
                      <small role="alert">
                        Не удалось загрузить старые запуски: {historyLoadMoreError}
                      </small>
                    )}
                    {!historyLoading && !historyFailed && historyHasMore && (
                      <button
                        className="button secondary"
                        disabled={historyLoadingMore}
                        onClick={() => void loadOlderHistory(automation.id)}
                      >
                        {historyLoadingMore
                          ? 'Загружаю старые…'
                          : historyLoadMoreError
                            ? 'Повторить загрузку'
                            : 'Показать старые'}
                      </button>
                    )}
                  </div>
                )}
              </div>
            </article>
          );
        })}
      </AsyncRegion>
    </section>
  );
}
