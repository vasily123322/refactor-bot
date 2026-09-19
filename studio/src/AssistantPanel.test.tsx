import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import {
  AssistantBrief,
  AssistantPanel,
  mergeAssistantApproval,
  mergeAssistantRun,
} from './AssistantPanel';
import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveChannelDataView,
  runExclusiveOperation,
} from './asyncControl';
import type { AssistantApprovalView, AssistantRunView } from './api';
import type { Channel } from './types';

function attentionRunView(summary: string): AssistantRunView {
  return {
    id: 11,
    channel_id: 7,
    scenario: 'attention_today',
    request_id: null,
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

  it('repeated CTA click starts only one execution while unrelated code can continue', async () => {
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

  it('renders exactly three draft references with existing editor action and long Russian title', () => {
    const html = renderToStaticMarkup(<AssistantBrief run={draftRunView()} />);
    expect(html.match(/Открыть в редакторе/g)).toHaveLength(3);
    expect(html.match(/Черновик/g)).toHaveLength(3);
    expect(html).toContain('Content #101');
    expect(html).toContain('Content #102');
    expect(html).toContain('Content #103');
    expect(html).toContain('2026-09-20');
    expect(html).toContain('Очень длинный русский заголовок');
  });

  it('proposal form is explicit and does not claim scheduled state before review', () => {
    const html = renderToStaticMarkup(<AssistantBrief run={draftRunView()} />);
    expect(html.match(/type="time"/g)).toHaveLength(3);
    expect(html.match(/Создать предложение/g)).toHaveLength(3);
    expect(html).toContain('На этом шаге расписание не меняется');
    expect(html).not.toContain('Поставлено в план');
    expect(html).not.toContain('ScheduleEntry #501');
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
