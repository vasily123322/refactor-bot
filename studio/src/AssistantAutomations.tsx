import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { StudioApiError, studioApi } from './api';
import type {
  AssistantAutomationCreateInput,
  AssistantAutomationView,
  AssistantSkillView,
} from './api';
import { AsyncRegion, SkeletonBlock } from './AsyncUI';
import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveChannelDataView,
  runExclusiveOperation,
  type ChannelLoadState,
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
  const [toggleBusyIds, setToggleBusyIds] = useState<Set<number>>(() => new Set());

  const channelIdRef = useRef(channel.id);
  const loadOwnershipRef = useRef(new ChannelRequestOwnership());
  const createOwnershipRef = useRef(new ChannelRequestOwnership());
  const createLockRef = useRef(new ExclusiveOperationLock());
  const toggleLocksRef = useRef(new Map<number, ExclusiveOperationLock>());
  const validDataChannelRef = useRef<number | null>(null);
  const pendingCreateRef = useRef<{
    channelId: number;
    fingerprint: string;
    requestId: string;
  } | null>(null);
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
    setToggleBusyIds(new Set());
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
    let lock = toggleLocksRef.current.get(automation.id);
    if (!lock) {
      lock = new ExclusiveOperationLock();
      toggleLocksRef.current.set(automation.id, lock);
    }
    const result = await runExclusiveOperation(
      lock,
      `toggle:${automation.id}`,
      async () => {
        setToggleBusyIds((previous) => new Set(previous).add(automation.id));
        setError(null);
        try {
          const updated = await studioApi.setAssistantAutomationEnabled(
            channelId,
            automation.id,
            !automation.enabled,
          );
          if (channelIdRef.current !== channelId) return;
          setAutomations((previous) => mergeAssistantAutomation(previous, updated));
        } catch (reason) {
          if (channelIdRef.current === channelId) {
            setError({ channelId, message: automationErrorMessage(reason) });
          }
        } finally {
          if (channelIdRef.current === channelId) {
            setToggleBusyIds((previous) => {
              const next = new Set(previous);
              next.delete(automation.id);
              return next;
            });
          }
        }
      },
    );
    if (!result.started) return;
  }, []);

  const loadView = resolveChannelDataView(loadState, channel.id, automations.length);
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
          <button onClick={() => setError(null)}>×</button>
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
        {automations.map((automation) => (
          <article className="assistant-automation-row" key={automation.id}>
            <div>
              <strong>{automation.skill_id}@{automation.skill_version}</strong>
              <small>
                {automationCadenceLabel(automation)} · {automation.timezone}
              </small>
              <small>
                Следующий запуск: {nextRunLabel(automation.next_run_at)}
              </small>
            </div>
            <button
              className="button secondary"
              disabled={toggleBusyIds.has(automation.id)}
              onClick={() => void toggleEnabled(automation)}
            >
              {toggleBusyIds.has(automation.id)
                ? 'Сохраняю…'
                : automation.enabled ? 'Отключить' : 'Включить'}
            </button>
          </article>
        ))}
      </AsyncRegion>
    </section>
  );
}
