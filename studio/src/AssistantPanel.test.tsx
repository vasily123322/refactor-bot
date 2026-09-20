import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import {
  AssistantBrief,
  AssistantPanel,
  AssistantSkillCatalog,
  mergeAssistantApproval,
  mergeAssistantRun,
  mergeAssistantSeriesApproval,
  reconcileAssistantApprovalDecisionFailure,
  shouldReconcileAssistantApprovalDecision,
} from './AssistantPanel';
import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveChannelDataView,
  runExclusiveOperation,
} from './asyncControl';
import { StudioApiError } from './api';
import type {
  AssistantApprovalView,
  AssistantRunView,
  AssistantSeriesApprovalView,
  AssistantSkillView,
} from './api';
import type { Channel } from './types';

function attentionRunView(summary: string): AssistantRunView {
  return {
    id: 11,
    channel_id: 7,
    scenario: 'attention_today',
    request_id: null,
    operator_input: null,
    skill_id: 'attention_today',
    skill_version: '1',
    workflow_phase: 'completed',
    resumable: false,
    resume_state: 'completed',
    status: 'completed',
    model: 'provider/model',
    tokens_used: 12,
    error: null,
    started_at: '2026-09-19T00:00:00Z',
    finished_at: '2026-09-19T00:00:01Z',
    created_at: '2026-09-19T00:00:00Z',
    events: [],
    result: {
      scenario: 'attention_today',
      summary,
      attention_items: [
        {
          fact_id: 'schedule:4:overdue',
          category: 'schedule',
          severity: 'high',
          title: 'Просрочена ожидающая публикация',
          detail: 'Длинный русский operational текст без HTML-инъекции.',
          refs: { schedule_entry_id: 4, publication_id: 9, content_item_id: 12 },
          suggested_action: 'Проверьте запись в Планере.',
        },
      ],
      timezone: 'Europe/Berlin',
      generated_by: 'llm_priority',
      tool_names: ['schedule_attention'],
      execution_limits: {
        max_steps: 5,
        max_tool_calls: 4,
        max_llm_calls: 1,
        max_seconds: 20,
      },
    },
  };
}

function draftRunView(): AssistantRunView {
  const longTitle = 'Очень длинный русский заголовок черновика для проверки переноса '.repeat(5);
  return {
    id: 22,
    channel_id: 7,
    scenario: 'drafts_tomorrow',
    request_id: 'draft-request-22',
    operator_input: null,
    skill_id: 'drafts_tomorrow',
    skill_version: '1',
    workflow_phase: 'completed',
    resumable: false,
    resume_state: 'completed',
    status: 'completed',
    model: 'provider/model',
    tokens_used: 101,
    error: null,
    started_at: '2026-09-19T00:00:00Z',
    finished_at: '2026-09-19T00:00:02Z',
    created_at: '2026-09-19T00:00:00Z',
    events: [],
    result: {
      scenario: 'drafts_tomorrow',
      target_local_date: '2026-09-20',
      timezone: 'Europe/Berlin',
      draft_count: 3,
      drafts: [
        {
          content_item_id: 101,
          content_revision: 1,
          title: longTitle,
          status: 'draft',
        },
        {
          content_item_id: 102,
          content_revision: 1,
          title: 'Второй самостоятельный угол',
          status: 'draft',
        },
        {
          content_item_id: 103,
          content_revision: 1,
          title: 'Третий самостоятельный угол',
          status: 'draft',
        },
      ],
      write_capability: 'draft_write',
      editorial_context: {
        recent_count: 5,
        scheduled_count: 2,
        item_count: 7,
        total_excerpt_chars: 1200,
        fingerprint: 'c'.repeat(64),
        refs: [{ content_item_id: 91, revision: 2 }],
      },
      execution_limits: {
        max_steps: 4,
        max_tool_calls: 0,
        max_llm_calls: 1,
        max_seconds: 30,
      },
    },
  };
}

function skillViews(): AssistantSkillView[] {
  return [
    {
      skill_id: 'attention_today',
      version: '1',
      scenario: 'attention_today',
      display_title: 'Что сегодня требует внимания?',
      description: 'Показывает bounded operational snapshot.',
      category: 'operations',
      operator_input_schema: {
        type: 'object',
        properties: {},
        required: [],
        additionalProperties: false,
      },
      result_kind: 'attention_brief',
      capability_classes: ['read_only'],
      capability_summary: 'Read-only: без Content writes.',
      context_profile: 'none',
      context_requirements: 'Channel-scoped operational state.',
      resume_policy: 'none',
      resumable: false,
      approval_requirement: 'none',
      execution_limits: {
        max_steps: 5,
        max_tool_calls: 4,
        max_llm_calls: 1,
        max_seconds: 20,
        max_items_per_tool: 20,
      },
    },
    {
      skill_id: 'drafts_tomorrow',
      version: '1',
      scenario: 'drafts_tomorrow',
      display_title: 'Создать 3 черновика на завтра',
      description: 'Создаёт ordinary Content drafts.',
      category: 'editorial',
      operator_input_schema: {
        type: 'object',
        properties: {},
        required: [],
        additionalProperties: false,
      },
      result_kind: 'content_drafts',
      capability_classes: ['draft_write'],
      capability_summary: 'Draft-write: scheduling отдельно.',
      context_profile: 'editorial_v1',
      context_requirements: 'Existing channel memory/profile + bounded Content context.',
      resume_policy: 'explicit',
      resumable: true,
      approval_requirement: 'none',
      execution_limits: {
        max_steps: 4,
        max_tool_calls: 0,
        max_llm_calls: 1,
        max_seconds: 30,
        max_items_per_tool: 0,
      },
    },
    {
      skill_id: 'prepare_content_series',
      version: '1',
      scenario: 'prepare_content_series',
      display_title: 'Подготовить серию постов',
      description: 'Создаёт bounded series plan и ordinary Content drafts.',
      category: 'editorial',
      operator_input_schema: {
        type: 'object',
        properties: {
          brief: { type: 'string', minLength: 20, maxLength: 2000 },
          post_count: { type: 'integer', minimum: 2, maximum: 8 },
        },
        required: ['brief', 'post_count'],
        additionalProperties: false,
      },
      result_kind: 'content_series',
      capability_classes: ['draft_write'],
      capability_summary: 'Draft-write: scheduling/publishing не выполняются.',
      context_profile: 'editorial_v1',
      context_requirements: 'Existing channel memory/profile + bounded Content context.',
      resume_policy: 'explicit',
      resumable: true,
      approval_requirement: 'none',
      execution_limits: {
        max_steps: 4,
        max_tool_calls: 0,
        max_llm_calls: 1,
        max_seconds: 30,
        max_items_per_tool: 0,
      },
    },
  ];
}

function seriesRunView(count = 8): AssistantRunView {
  const longAngle = 'Очень длинный русский редакционный угол для проверки переноса '.repeat(4);
  return {
    id: 33,
    channel_id: 7,
    scenario: 'prepare_content_series',
    request_id: 'series-request-33',
    operator_input: {
      brief: 'Подготовь bounded evergreen серию с самостоятельными углами.',
      post_count: count,
    },
    skill_id: 'prepare_content_series',
    skill_version: '1',
    workflow_phase: 'completed',
    resumable: false,
    resume_state: 'completed',
    status: 'completed',
    model: 'provider/model',
    tokens_used: 88,
    error: null,
    started_at: '2026-09-19T00:00:00Z',
    finished_at: '2026-09-19T00:00:03Z',
    created_at: '2026-09-19T00:00:00Z',
    events: [],
    result: {
      scenario: 'prepare_content_series',
      series_title: 'Очень длинное название серии для редакционного плана',
      series_summary: 'Bounded evergreen summary для всей серии.',
      requested_post_count: count,
      plan_fingerprint: 'd'.repeat(64),
      posts: Array.from({ length: count }, (_, index) => ({
        ordinal: index + 1,
        title: `Пост ${index + 1}: самостоятельный заголовок`,
        angle: `${longAngle}${index + 1}`,
        objective: `Самостоятельная цель ${index + 1}`,
        content_item_id: 200 + index,
        content_revision: 1,
        status: 'draft',
      })),
      write_capability: 'draft_write',
      editorial_context: {
        recent_count: 5,
        scheduled_count: 3,
        item_count: 8,
        total_excerpt_chars: 1800,
        fingerprint: 'e'.repeat(64),
        refs: [],
      },
      execution_limits: {
        max_steps: 4,
        max_tool_calls: 0,
        max_llm_calls: 1,
        max_seconds: 30,
      },
    },
  };
}


function approvalView(
  state: AssistantApprovalView['state'] = 'pending_review',
): AssistantApprovalView {
  return {
    id: 71,
    channel_id: 7,
    source_admin_agent_run_id: 22,
    action_type: 'schedule_draft_tomorrow',
    state,
    content_item_id: 101,
    content_revision: 1,
    timezone: 'Europe/Berlin',
    target_local_date: '2026-09-20',
    local_time: '14:30',
    resolved_scheduled_at: '2026-09-20T12:30:00Z',
    action_fingerprint: 'a'.repeat(64),
    execution_key: state === 'pending_review' ? null : 'b'.repeat(64),
    request_id: 'approval-request-71',
    schedule_entry_id: state === 'executed' ? 501 : null,
    publication_id: state === 'executed' ? 601 : null,
    reviewer_tg_user_id: state === 'pending_review' ? null : 7001,
    failure_reason: state === 'stale' ? 'draft revision changed' : null,
    reviewed_at: state === 'pending_review' ? null : '2026-09-19T01:00:00Z',
    executed_at: state === 'executed' ? '2026-09-19T01:00:01Z' : null,
    created_at: '2026-09-19T00:30:00Z',
  };
}


function seriesApprovalView(
  state: AssistantSeriesApprovalView['state'] = 'pending_review',
  count = 8,
): AssistantSeriesApprovalView {
  const partial = state === 'partial_failed';
  return {
    id: 81,
    channel_id: 7,
    source_run_id: 33,
    action_type: 'schedule_content_series',
    state,
    request_id: 'series-approval-request-81',
    timezone: 'Europe/Berlin',
    item_count: count,
    series_title: 'Очень длинное название серии для редакционного плана',
    source_plan_fingerprint: 'd'.repeat(64),
    action_fingerprint: 'f'.repeat(64),
    execution_key: '1'.repeat(64),
    reviewer_tg_user_id: state === 'pending_review' ? null : 7001,
    failure_reason: partial ? 'ordinal 3: content revision changed' : null,
    reviewed_at: state === 'pending_review' ? null : '2026-09-19T01:00:00Z',
    executed_at: state === 'executed' ? '2026-09-19T01:00:02Z' : null,
    created_at: '2026-09-19T00:40:00Z',
    items: Array.from({ length: count }, (_, index) => {
      const ordinal = index + 1;
      const itemExecuted = state === 'executed' || (partial && ordinal <= 2);
      const itemStale = partial && ordinal === 3;
      return {
        id: 810 + ordinal,
        ordinal,
        content_item_id: 200 + index,
        captured_content_revision: ordinal === 2 ? 2 : 1,
        content_title: `Пост ${ordinal}: очень длинный русский заголовок для mobile review`,
        local_date: '2026-09-20',
        local_time: `${12 + ordinal}:30`,
        resolved_scheduled_at: `2026-09-20T${10 + ordinal}:30:00Z`,
        item_fingerprint: String(ordinal).repeat(64).slice(0, 64),
        execution_key: String(ordinal + 1).repeat(64).slice(0, 64),
        state: itemExecuted ? 'executed' : itemStale ? 'stale' : 'pending',
        schedule_entry_id: itemExecuted ? 500 + ordinal : null,
        publication_id: itemExecuted ? 600 + ordinal : null,
        failure_reason: itemStale ? 'content revision changed' : null,
        execution_started_at: itemExecuted ? '2026-09-19T01:00:01Z' : null,
        executed_at: itemExecuted ? '2026-09-19T01:00:02Z' : null,
      };
    }),
  };
}

describe('Assistant async ownership', () => {
  it('distinguishes initial loading, empty history and error', () => {
    expect(resolveChannelDataView(null, 7, 0)).toBe('loading');
    expect(resolveChannelDataView({ channelId: 7, phase: 'loaded' }, 7, 0)).toBe('loaded-empty');
    expect(resolveChannelDataView({ channelId: 7, phase: 'error-without-valid-data' }, 7, 0))
      .toBe('error-without-valid-data');
  });

  it('ignores a stale run response after channel switch', () => {
    const ownership = new ChannelRequestOwnership();
    const oldChannel = ownership.begin(1);
    const newChannel = ownership.begin(2);
    expect(ownership.isCurrent(oldChannel, 2)).toBe(false);
    expect(ownership.isCurrent(newChannel, 2)).toBe(true);
  });

  it('catalog launcher preserves the one-request execution lock', async () => {
    const lock = new ExclusiveOperationLock();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let calls = 0;
    let unrelated = 0;

    const first = runExclusiveOperation(lock, 'drafts_tomorrow', async () => {
      calls += 1;
      await gate;
    });
    const duplicate = await runExclusiveOperation(lock, 'drafts_tomorrow', async () => {
      calls += 1;
    });
    unrelated += 1;

    expect(duplicate.started).toBe(false);
    expect(calls).toBe(1);
    expect(unrelated).toBe(1);
    release();
    await first;
  });

  it('idempotent response replaces the same run instead of duplicating cards/history', () => {
    const run = draftRunView();
    const first = mergeAssistantRun([], run);
    const retry = mergeAssistantRun(first, { ...run });
    expect(retry).toHaveLength(1);
    expect(retry[0].id).toBe(run.id);
  });

  it('approval merge replaces an idempotent retry instead of duplicating review cards', () => {
    const approval = approvalView();
    const first = mergeAssistantApproval([], approval);
    const retry = mergeAssistantApproval(first, { ...approval });
    expect(retry).toHaveLength(1);
    expect(retry[0].id).toBe(approval.id);
  });

  it('approval merge keeps only the latest association for one draft revision', () => {
    const older = approvalView('rejected');
    const newer = {
      ...approvalView(),
      id: 72,
      request_id: 'approval-request-72',
    };
    const merged = mergeAssistantApproval([older], newer);
    expect(merged).toHaveLength(1);
    expect(merged[0].id).toBe(72);
    expect(merged[0].content_item_id).toBe(older.content_item_id);
    expect(merged[0].content_revision).toBe(older.content_revision);
  });

  it('approval merge preserves associations for distinct revisions of one content item', () => {
    const revisionOne = approvalView('rejected');
    const revisionTwo = {
      ...approvalView(),
      id: 73,
      content_revision: 2,
      request_id: 'approval-request-73',
    };
    const merged = mergeAssistantApproval([revisionOne], revisionTwo);
    expect(merged).toHaveLength(2);
    expect(merged.map((row) => row.content_revision)).toEqual([2, 1]);
  });

  it('rejects a stale targeted approval response after run switch', () => {
    const ownership = new ChannelRequestOwnership();
    const oldRun = ownership.begin(7, '22');
    const newRun = ownership.begin(7, '23');
    expect(ownership.isCurrent(oldRun, 7, '23')).toBe(false);
    expect(ownership.isCurrent(newRun, 7, '23')).toBe(true);
  });


  it('series approval merge replaces an idempotent retry instead of duplicating review cards', () => {
    const approval = seriesApprovalView();
    const first = mergeAssistantSeriesApproval([], approval);
    const retry = mergeAssistantSeriesApproval(first, { ...approval });
    expect(retry).toHaveLength(1);
    expect(retry[0].id).toBe(approval.id);
  });

  it('series approval merge keeps only the latest association for one source run', () => {
    const older = seriesApprovalView('rejected');
    const newer = {
      ...seriesApprovalView(),
      id: 82,
      request_id: 'series-approval-request-82',
    };
    const merged = mergeAssistantSeriesApproval([older], newer);
    expect(merged).toHaveLength(1);
    expect(merged[0].id).toBe(82);
    expect(merged[0].source_run_id).toBe(older.source_run_id);
  });

  it('rejects a stale targeted series approval response after run switch', () => {
    const ownership = new ChannelRequestOwnership();
    const oldRun = ownership.begin(7, '33');
    const newRun = ownership.begin(7, '34');
    expect(ownership.isCurrent(oldRun, 7, '34')).toBe(false);
    expect(ownership.isCurrent(newRun, 7, '34')).toBe(true);
  });

  it('reconciles authoritative approval scope after a conflict decision response', async () => {
    const conflict = new StudioApiError('approval state changed', 409);
    let reloads = 0;

    expect(shouldReconcileAssistantApprovalDecision(conflict)).toBe(true);
    await reconcileAssistantApprovalDecisionFailure(
      conflict,
      () => true,
      async () => { reloads += 1; },
    );

    expect(reloads).toBe(1);
  });

  it('reconciles authoritative approval scope after a failed server decision response', async () => {
    const failed = new StudioApiError('canonical scheduling attempt failed', 503);
    let reloads = 0;

    expect(shouldReconcileAssistantApprovalDecision(failed)).toBe(true);
    await reconcileAssistantApprovalDecisionFailure(
      failed,
      () => true,
      async () => { reloads += 1; },
    );

    expect(reloads).toBe(1);
  });

  it('does not reconcile an old approval decision after run ownership moved', async () => {
    const conflict = new StudioApiError('approval state changed', 409);
    const ownership = new ChannelRequestOwnership();
    const oldRun = ownership.begin(7, '22');
    ownership.begin(7, '23');
    let reloads = 0;

    await reconcileAssistantApprovalDecisionFailure(
      conflict,
      () => ownership.isCurrent(oldRun, 7, '23'),
      async () => { reloads += 1; },
    );

    expect(reloads).toBe(0);
  });

  it('does not reconcile validation failures that cannot represent changed decision state', async () => {
    const invalid = new StudioApiError('invalid request', 422);
    let reloads = 0;

    expect(shouldReconcileAssistantApprovalDecision(invalid)).toBe(false);
    await reconcileAssistantApprovalDecisionFailure(
      invalid,
      () => true,
      async () => { reloads += 1; },
    );

    expect(reloads).toBe(0);
  });

  it('coalesces repeated series proposal and approve clicks independently', async () => {
    const proposalLock = new ExclusiveOperationLock();
    const approveLock = new ExclusiveOperationLock();
    let releaseProposal!: () => void;
    let releaseApprove!: () => void;
    const proposalGate = new Promise<void>((resolve) => { releaseProposal = resolve; });
    const approveGate = new Promise<void>((resolve) => { releaseApprove = resolve; });
    let proposalCalls = 0;
    let approveCalls = 0;

    const firstProposal = runExclusiveOperation(
      proposalLock,
      'series-proposal:33',
      async () => {
        proposalCalls += 1;
        await proposalGate;
      },
    );
    const duplicateProposal = await runExclusiveOperation(
      proposalLock,
      'series-proposal:33',
      async () => {
        proposalCalls += 1;
      },
    );
    const firstApprove = runExclusiveOperation(
      approveLock,
      'series-approval:81',
      async () => {
        approveCalls += 1;
        await approveGate;
      },
    );
    const duplicateApprove = await runExclusiveOperation(
      approveLock,
      'series-approval:81',
      async () => {
        approveCalls += 1;
      },
    );

    expect(duplicateProposal.started).toBe(false);
    expect(duplicateApprove.started).toBe(false);
    expect(proposalCalls).toBe(1);
    expect(approveCalls).toBe(1);
    releaseProposal();
    releaseApprove();
    await Promise.all([firstProposal, firstApprove]);
  });

  it('locks one approval operation without globally blocking an unrelated draft', async () => {
    const firstDraftLock = new ExclusiveOperationLock();
    const secondDraftLock = new ExclusiveOperationLock();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let firstCalls = 0;
    let secondCalls = 0;

    const first = runExclusiveOperation(firstDraftLock, 'proposal:101', async () => {
      firstCalls += 1;
      await gate;
    });
    const duplicate = await runExclusiveOperation(firstDraftLock, 'proposal:101', async () => {
      firstCalls += 1;
    });
    const unrelated = await runExclusiveOperation(secondDraftLock, 'proposal:102', async () => {
      secondCalls += 1;
    });

    expect(duplicate.started).toBe(false);
    expect(unrelated.started).toBe(true);
    expect(firstCalls).toBe(1);
    expect(secondCalls).toBe(1);
    release();
    await first;
  });
});

describe('Assistant skill catalog rendering', () => {
  it('shows current metadata, capabilities, resumability, approval and primary limits', () => {
    const html = renderToStaticMarkup(
      <AssistantSkillCatalog
        skills={skillViews()}
        loading={false}
        errorMessage={null}
        runningScenario={null}
        onRun={() => undefined}
      />,
    );
    expect(html).toContain('attention_today@1');
    expect(html).toContain('drafts_tomorrow@1');
    expect(html).toContain('prepare_content_series@1');
    expect(html).toContain('Brief серии');
    expect(html).toContain('Количество постов');
    expect(html).toContain('Подготовить серию');
    expect(html).toContain('minLength="20"');
    expect(html).toContain('maxLength="2000"');
    expect(html).toContain('read-only');
    expect(html).toContain('draft-write');
    expect(html).toContain('explicit');
    expect(html).toContain('для запуска не требуется');
    expect(html).toContain('LLM ≤ 1');
    expect(html).toContain('time ≤ 30s');
    expect(html).toContain('Existing channel memory/profile');
    expect(html.match(/Запустить/g)).toHaveLength(2);
  });

  it('has distinct loading, error and empty catalog states', () => {
    const loading = renderToStaticMarkup(
      <AssistantSkillCatalog
        skills={[]}
        loading
        errorMessage={null}
        runningScenario={null}
      />,
    );
    const error = renderToStaticMarkup(
      <AssistantSkillCatalog
        skills={[]}
        loading={false}
        errorMessage="catalog unavailable"
        runningScenario={null}
      />,
    );
    const empty = renderToStaticMarkup(
      <AssistantSkillCatalog
        skills={[]}
        loading={false}
        errorMessage={null}
        runningScenario={null}
      />,
    );
    expect(loading).toContain('Загружаю каталог навыков');
    expect(error).toContain('Каталог навыков не загружен');
    expect(error).toContain('catalog unavailable');
    expect(empty).toContain('Каталог навыков пуст');
  });

  it('stale catalog ownership is rejected after channel switch', () => {
    const ownership = new ChannelRequestOwnership();
    const oldCatalog = ownership.begin(1);
    const newCatalog = ownership.begin(2);
    expect(ownership.isCurrent(oldCatalog, 2)).toBe(false);
    expect(ownership.isCurrent(newCatalog, 2)).toBe(true);
  });
});

describe('Assistant scenario rendering', () => {
  it('renders both explicit scenario CTAs', () => {
    const channel = {
      id: 7,
      tg_chat_id: -1007,
      title: 'Редакционный канал',
    } as Channel;
    const html = renderToStaticMarkup(<AssistantPanel channel={channel} />);
    expect(html).toContain('Что сегодня требует внимания?');
    expect(html).toContain('Создать 3 черновика на завтра');
  });

  it('renders completed attention facts and escapes long Russian/HTML-like text', () => {
    const unsafe = '<img src=x onerror=alert(1)> Очень длинный русский результат '.repeat(12);
    const html = renderToStaticMarkup(<AssistantBrief run={attentionRunView(unsafe)} />);
    expect(html).toContain('Просрочена ожидающая публикация');
    expect(html).toContain('ScheduleEntry #4');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(html).not.toContain('<img src=x onerror=alert(1)>');
  });

  it('renders an eight-item series with exact bounded scheduling controls and no false scheduled state', () => {
    const html = renderToStaticMarkup(<AssistantBrief run={seriesRunView()} />);
    expect(html).toContain('Очень длинное название серии');
    expect(html).toContain('Bounded evergreen summary');
    expect(html.match(/Открыть в редакторе/g)).toHaveLength(8);
    expect(html.match(/Content #20/g)?.length ?? 0).toBeGreaterThan(0);
    expect(html).toContain('Угол:');
    expect(html).toContain('Цель:');
    expect(html.match(/type="date"/g)).toHaveLength(8);
    expect(html.match(/type="time"/g)).toHaveLength(8);
    expect(html).toContain('Создать предложение расписания');
    expect(html).toContain('ScheduleEntry и Publication не создаются');
    expect(html).not.toContain('Поставлено в план');
    expect(html).not.toContain('Telegram сейчас не отправляется');
  });

  it('does not offer a fresh series proposal before targeted association is loaded', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief run={seriesRunView()} seriesApprovalsReady={false} />,
    );
    expect(html).not.toContain('Создать предложение расписания');
    expect(html).not.toContain('type="date"');
    expect(html).not.toContain('type="time"');
  });

  it('series review card shows exact batch effect, captured revisions and explicit decisions', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief
        run={seriesRunView()}
        seriesApprovals={[seriesApprovalView()]}
      />,
    );
    expect(html).toContain('Ожидает подтверждения');
    expect(html).toContain('run #33');
    expect(html).toContain('fingerprint dddddddddddd');
    expect(html).toContain('8 постов · Europe/Berlin');
    expect(html).toContain(
      'После подтверждения будет создано 8 canonical ScheduleEntry + 8 Publication.',
    );
    expect(html).toContain('Telegram сейчас не отправляется');
    expect(html).toContain('существующим canonical worker');
    expect(html).toContain('captured revision 2');
    expect(html).toContain('Подтвердить постановку серии в план');
    expect(html).toContain('Отклонить');
    expect(html).not.toContain('Поставлено в план');
  });

  it('executed series review shows every canonical pair and Planner action', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief
        run={seriesRunView()}
        seriesApprovals={[seriesApprovalView('executed')]}
      />,
    );
    expect(html).toContain('Поставлено в план');
    expect(html.match(/ScheduleEntry #50/g)?.length ?? 0).toBeGreaterThan(0);
    expect(html.match(/Publication #60/g)?.length ?? 0).toBeGreaterThan(0);
    expect(html).toContain('ScheduleEntry #508');
    expect(html).toContain('Publication #608');
    expect(html).toContain('Открыть Planner');
    expect(html).not.toContain('Подтвердить постановку серии в план');
  });

  it('partial_failed series review distinguishes executed, stale and pending ordinals textually', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief
        run={seriesRunView()}
        seriesApprovals={[seriesApprovalView('partial_failed')]}
      />,
    );
    expect(html).toContain('Частично выполнено');
    expect(html).toContain('ScheduleEntry #501');
    expect(html).toContain('ScheduleEntry #502');
    expect(html).toContain('3. Пост 3');
    expect(html).toContain('stale');
    expect(html).toContain('content revision changed');
    expect(html).toContain('4. Пост 4');
    expect(html).toContain('pending');
    expect(html).toContain('role="alert"');
    expect(html).not.toContain('Создать предложение расписания');
    expect(html).toContain('Открыть Planner');
  });

  it('rejected series review is terminal and offers a fresh explicit proposal surface', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief
        run={seriesRunView()}
        seriesApprovals={[seriesApprovalView('rejected')]}
      />,
    );
    expect(html).toContain('Отклонено');
    expect(html).toContain('Создать предложение расписания');
    expect(html).not.toContain('Подтвердить постановку серии в план');
  });

  it('renders exactly three draft references with existing editor action and long Russian title', () => {
    const html = renderToStaticMarkup(<AssistantBrief run={draftRunView()} />);
    expect(html.match(/Открыть в редакторе/g)).toHaveLength(3);
    expect(html.match(/Черновик/g)).toHaveLength(3);
    expect(html).toContain('Content #101');
    expect(html).toContain('Content #102');
    expect(html).toContain('Content #103');
    expect(html).toContain('2026-09-20');
    expect(html).toContain('Очень длинный русский заголовок');
    expect(html).toContain('Bounded context: 5 recent · 2 scheduled refs');
  });

  it('proposal form is explicit and does not claim scheduled state before review', () => {
    const html = renderToStaticMarkup(<AssistantBrief run={draftRunView()} />);
    expect(html.match(/type="time"/g)).toHaveLength(3);
    expect(html.match(/Создать предложение/g)).toHaveLength(3);
    expect(html).toContain('На этом шаге расписание не меняется');
    expect(html).not.toContain('Поставлено в план');
    expect(html).not.toContain('ScheduleEntry #501');
  });

  it('does not offer a fresh proposal before targeted approval association is loaded', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief run={draftRunView()} draftApprovalsReady={false} />,
    );
    expect(html).not.toContain('Создать предложение');
    expect(html).not.toContain('type="time"');
  });

  it('draft card ignores an approval for another revision of the same content item', () => {
    const wrongRevision = {
      ...approvalView(),
      content_revision: 2,
    };
    const html = renderToStaticMarkup(
      <AssistantBrief run={draftRunView()} approvals={[wrongRevision]} />,
    );
    expect(html).toContain('Content #101 · revision 1');
    expect(html).toContain('Создать предложение');
    expect(html).not.toContain('Content #101, revision 2');
  });

  it('pending review card shows exact effect, provider boundary and explicit decisions', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief run={draftRunView()} approvals={[approvalView()]} />,
    );
    expect(html).toContain('Ожидает подтверждения');
    expect(html).toContain('Content #101, revision 1');
    expect(html).toContain('2026-09-20 · 14:30 · Europe/Berlin');
    expect(html).toContain('Будут созданы ScheduleEntry + Publication');
    expect(html).toContain('Telegram сейчас не отправляется');
    expect(html).toContain('canonical delivery worker');
    expect(html).toContain('Подтвердить постановку в план');
    expect(html).toContain('Отклонить');
    expect(html).not.toContain('Поставлено в план');
  });

  it('rejected proposal renders an explicit textual terminal state', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief run={draftRunView()} approvals={[approvalView('rejected')]} />,
    );
    expect(html).toContain('Отклонено');
    expect(html).toContain('Создать предложение');
    expect(html).not.toContain('Подтвердить постановку в план');
  });

  it('stale proposal is textual, recoverable by a new proposal, and not color-only', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief run={draftRunView()} approvals={[approvalView('stale')]} />,
    );
    expect(html).toContain('Устарело');
    expect(html).toContain('draft revision changed');
    expect(html).toContain('Создать предложение');
    expect(html).not.toContain('Подтвердить постановку в план');
  });

  it('executed proposal renders canonical IDs and Planner action', () => {
    const html = renderToStaticMarkup(
      <AssistantBrief run={draftRunView()} approvals={[approvalView('executed')]} />,
    );
    expect(html).toContain('Поставлено в план');
    expect(html).toContain('ScheduleEntry #501');
    expect(html).toContain('Publication #601');
    expect(html).toContain('Открыть Planner');
    expect(html).not.toContain('Подтвердить постановку в план');
  });

  it('failed draft run is accessible and never renders false created draft cards', () => {
    const failed: AssistantRunView = {
      ...draftRunView(),
      status: 'failed',
      error: 'admin agent execution failed',
      result: null,
    };
    const html = renderToStaticMarkup(<AssistantBrief run={failed} />);
    expect(html).toContain('role="alert"');
    expect(html).toContain('admin agent execution failed');
    expect(html).not.toContain('Открыть в редакторе');
    expect(html).not.toContain('Content #101');
  });
});
