import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import {
  AssistantAutomations,
  automationCadenceLabel,
  automationDataView,
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
    timezone: 'Europe/Berlin',
    enabled: true,
    next_run_at: '2026-09-23T07:30:00Z',
    last_scheduled_for: null,
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
