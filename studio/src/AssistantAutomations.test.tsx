import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import {
  AssistantAutomations,
  automationAvailableControls,
  automationCadenceLabel,
  automationDataView,
  automationHealthLabel,
  automationScheduleSummary,
  eligibleAutomationSkills,
  mergeAssistantAutomation,
  validateAutomationForm,
} from './AssistantAutomations';
import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  runExclusiveOperation,
} from './asyncControl';
import type {
  AssistantAutomationView,
  AssistantSkillView,
} from './api';
import type { Channel } from './types';


function skill(
  skillId: string,
  scenario: AssistantSkillView['scenario'],
  automationAllowed: boolean,
): AssistantSkillView {
  return {
    skill_id: skillId,
    version: '1',
    scenario,
    display_title: skillId,
    description: `${skillId} description`,
    category: 'editorial',
    operator_input_schema: {},
    result_kind: 'test',
    capability_classes: scenario === 'attention_today' ? ['read_only'] : ['draft_write'],
    capability_summary: 'bounded',
    context_profile: 'none',
    context_requirements: 'bounded',
    resume_policy: 'none',
    resumable: false,
    approval_requirement: 'none',
    automation_policy: automationAllowed ? 'bounded' : 'disabled',
    automation_allowed: automationAllowed,
    execution_limits: {
      max_steps: 4,
      max_tool_calls: 1,
      max_llm_calls: 1,
      max_seconds: 20,
    },
  };
}

function automation(id = 1): AssistantAutomationView {
  return {
    id,
    channel_id: 7,
    skill_id: 'attention_today',
    skill_version: '1',
    operator_input: {},
    cadence: {
      kind: 'weekly',
      local_time: '09:30',
      weekday: 2,
    },
    automation_policy: 'bounded',
    timezone: 'Europe/Berlin',
    enabled: true,
    disabled_reason: null,
    disabled_at: null,
    next_run_at: '2026-09-23T07:30:00Z',
    last_scheduled_for: null,
    last_outcome: 'run_recorded',
    last_outcome_at: '2026-09-19T08:00:00Z',
    health: 'active',
    health_reason: 'healthy',
    claim_active: false,
    claimed_at: null,
    latest_run: null,
    usage_7d: {
      occurrence_runs: 1,
      completed: 1,
      failed: 0,
      restart_required_or_manual_resume: 0,
      tokens_used: 17,
    },
    usage_30d: {
      occurrence_runs: 2,
      completed: 2,
      failed: 0,
      restart_required_or_manual_resume: 0,
      tokens_used: 29,
    },
    execution_limits: {
      max_steps: 4,
      max_tool_calls: 0,
      max_llm_calls: 1,
      max_seconds: 20,
    },
    cadence_occurrences_per_week: 1,
    post_count: null,
    migration_available: false,
    suggested_skill_id: null,
    suggested_skill_version: null,
    request_id: `automation-request-${id}`,
    definition_fingerprint: 'a'.repeat(64),
    created_at: '2026-09-19T08:00:00Z',
    updated_at: '2026-09-19T08:00:00Z',
  };
}

describe('Assistant automations', () => {
  it('shows only backend opted-in exact skill versions', () => {
    const skills = [
      skill('attention_today', 'attention_today', true),
      skill('prepare_content_series', 'prepare_content_series', true),
      skill('future_mutation', 'attention_today', false),
    ];
    expect(eligibleAutomationSkills(skills).map((item) => item.skill_id)).toEqual([
      'attention_today',
      'prepare_content_series',
    ]);

    const channel: Channel = {
      id: 7,
      tg_chat_id: -1007,
      title: 'Owned',
    } as Channel;
    const html = renderToStaticMarkup(
      <AssistantAutomations channel={channel} skills={skills} />,
    );
    expect(html).toContain('attention_today@1');
    expect(html).toContain('prepare_content_series@1');
    expect(html).not.toContain('future_mutation');
    expect(html).not.toContain('cron');
  });

  it('renders the bounded series input only for the series skill', () => {
    const channel = {
      id: 7,
      tg_chat_id: -1007,
      title: 'Owned',
    } as Channel;
    const html = renderToStaticMarkup(
      <AssistantAutomations
        channel={channel}
        skills={[skill('prepare_content_series', 'prepare_content_series', true)]}
      />,
    );
    expect(html).toContain('Brief серии');
    expect(html).toContain('Количество постов');
    expect(html).toContain('daily');
    expect(html).toContain('weekly');
    expect(html).not.toContain('free-form');
  });

  it('validates daily/weekly cadence and bounded series input', () => {
    const attention = skill('attention_today', 'attention_today', true);
    expect(validateAutomationForm({
      skill: attention,
      cadenceKind: 'daily',
      localTime: '09:30',
      weekday: 0,
      seriesBrief: '',
      seriesPostCount: 3,
    })).toBeNull();
    expect(validateAutomationForm({
      skill: attention,
      cadenceKind: 'weekly',
      localTime: '25:00',
      weekday: 7,
      seriesBrief: '',
      seriesPostCount: 3,
    })).toContain('HH:MM');

    const series = skill('prepare_content_series', 'prepare_content_series', true);
    expect(validateAutomationForm({
      skill: series,
      cadenceKind: 'weekly',
      localTime: '10:15',
      weekday: 2,
      seriesBrief: 'коротко',
      seriesPostCount: 3,
    })).toContain('20–2000');
    expect(validateAutomationForm({
      skill: series,
      cadenceKind: 'weekly',
      localTime: '10:15',
      weekday: 2,
      seriesBrief: 'Достаточно длинный bounded brief для безопасной серии.',
      seriesPostCount: 8,
    })).toBeNull();
  });

  it('covers loading/error/empty channel views and stale response ownership', () => {
    expect(automationDataView({ channelId: 7, phase: 'loading' }, 7, 0)).toBe('loading');
    expect(
      automationDataView({ channelId: 7, phase: 'error-without-valid-data' }, 7, 0),
    ).toBe('error-without-valid-data');
    expect(automationDataView({ channelId: 7, phase: 'loaded' }, 7, 0)).toBe('loaded-empty');

    const ownership = new ChannelRequestOwnership();
    const first = ownership.begin(7);
    expect(ownership.isCurrent(first, 7)).toBe(true);
    ownership.begin(8);
    expect(ownership.isCurrent(first, 8)).toBe(false);
    ownership.invalidate();
    expect(ownership.isCurrent(first, 7)).toBe(false);
  });

  it('keeps a single create operation in flight', async () => {
    const lock = new ExclusiveOperationLock();
    let release: () => void = () => undefined;
    const first = runExclusiveOperation(lock, 'create:7', async () => {
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return 1;
    });
    await Promise.resolve();
    const second = await runExclusiveOperation(lock, 'create:7', async () => 2);
    expect(second.started).toBe(false);
    release();
    expect((await first).started).toBe(true);
  });

  it('serializes conflicting controls per automation without blocking unrelated work', async () => {
    const automationOne = new ExclusiveOperationLock();
    const automationTwo = new ExclusiveOperationLock();
    const create = new ExclusiveOperationLock();
    let releaseResume!: () => void;
    const resumeGate = new Promise<void>((resolve) => { releaseResume = resolve; });
    let sameAutomationToggle = 0;
    let unrelatedToggle = 0;
    let unrelatedCreate = 0;

    const resume = runExclusiveOperation(automationOne, 'resume:10', async () => {
      await resumeGate;
    });
    const toggleSame = await runExclusiveOperation(automationOne, 'toggle:10', async () => {
      sameAutomationToggle += 1;
    });
    const toggleOther = await runExclusiveOperation(automationTwo, 'toggle:11', async () => {
      unrelatedToggle += 1;
    });
    const createNew = await runExclusiveOperation(create, 'create:7', async () => {
      unrelatedCreate += 1;
    });

    expect(toggleSame.started).toBe(false);
    expect(sameAutomationToggle).toBe(0);
    expect(toggleOther.started).toBe(true);
    expect(unrelatedToggle).toBe(1);
    expect(createNew.started).toBe(true);
    expect(unrelatedCreate).toBe(1);

    releaseResume();
    await resume;
  });


  it('derives health labels and bounded controls without any retry action', () => {
    const active = automation(10);
    expect(automationHealthLabel(active)).toContain('Активна');
    expect(automationAvailableControls(active)).toEqual(['pause', 'history']);

    const paused: AssistantAutomationView = {
      ...active,
      enabled: false,
      disabled_reason: 'manual_pause',
      disabled_at: '2026-09-19T09:00:00Z',
      health: 'paused',
      health_reason: 'manual_pause',
    };
    expect(automationHealthLabel(paused)).toContain('Приостановлена');
    expect(automationAvailableControls(paused)).toEqual(['enable', 'history']);

    const blocked: AssistantAutomationView = {
      ...paused,
      disabled_reason: 'unsupported_skill_version',
      health: 'blocked',
      health_reason: 'unsupported_skill_version',
      migration_available: true,
      suggested_skill_id: 'attention_today',
      suggested_skill_version: '2',
    };
    expect(automationHealthLabel(blocked)).toContain('Заблокирована');
    expect(automationAvailableControls(blocked)).toEqual(['history', 'replacement']);
    expect(automationAvailableControls(blocked)).not.toContain('enable');

    const needsAttention: AssistantAutomationView = {
      ...active,
      health: 'needs_attention',
      health_reason: 'manual_resume_available',
      latest_run: {
        id: 101,
        scheduled_for: '2026-09-19T07:30:00Z',
        status: 'failed',
        workflow_phase: 'generation_validated',
        resumable: true,
        resume_state: 'available',
        tokens_used: 11,
        model: 'provider/model',
        created_at: '2026-09-19T07:30:00Z',
        started_at: '2026-09-19T07:30:00Z',
        finished_at: null,
        result_kind: 'content_drafts',
        result_metadata: {},
      },
    };
    expect(automationHealthLabel(needsAttention)).toContain('Требует внимания');
    expect(automationAvailableControls(needsAttention)).toEqual([
      'pause',
      'resume',
      'history',
    ]);
    expect(automationAvailableControls(needsAttention)).not.toContain('retry' as never);
  });

  it('invalidates stale automation-history ownership when selection changes', () => {
    const ownership = new ChannelRequestOwnership();
    const first = ownership.begin(7, '10');
    expect(ownership.isCurrent(first, 7, '10')).toBe(true);
    const second = ownership.begin(7, '11');
    expect(ownership.isCurrent(first, 7, '11')).toBe(false);
    expect(ownership.isCurrent(second, 7, '11')).toBe(true);
    expect(ownership.isCurrent(second, 8, '11')).toBe(false);
  });

  it('merges server-idempotent definitions and renders pinned cadence/timezone/next run', () => {
    const row = automation(3);
    const updated = { ...row, enabled: false };
    expect(mergeAssistantAutomation([row], updated)).toEqual([updated]);
    expect(automationCadenceLabel(row)).toContain('weekly');
    const summary = automationScheduleSummary(row);
    expect(summary).toContain('Ср');
    expect(summary).toContain('Europe/Berlin');
    expect(summary).toContain('Следующий запуск');
  });
});
