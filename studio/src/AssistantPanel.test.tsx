import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { AssistantBrief } from './AssistantPanel';
import {
  ChannelRequestOwnership,
  ExclusiveOperationLock,
  resolveChannelDataView,
  runExclusiveOperation,
} from './asyncControl';
import type { AssistantRunView } from './api';

function runView(summary: string): AssistantRunView {
  return {
    id: 11,
    channel_id: 7,
    scenario: 'attention_today',
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
      generated_by: 'llm',
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

    const first = runExclusiveOperation(lock, 'attention_today', async () => {
      calls += 1;
      await gate;
    });
    const duplicate = await runExclusiveOperation(lock, 'attention_today', async () => {
      calls += 1;
    });
    unrelated += 1;

    expect(duplicate.started).toBe(false);
    expect(calls).toBe(1);
    expect(unrelated).toBe(1);
    release();
    await first;
  });
});

describe('Assistant completed rendering', () => {
  it('renders completed run facts and escapes long Russian/HTML-like text', () => {
    const unsafe = '<img src=x onerror=alert(1)> Очень длинный русский результат '.repeat(12);
    const html = renderToStaticMarkup(<AssistantBrief run={runView(unsafe)} />);
    expect(html).toContain('Просрочена ожидающая публикация');
    expect(html).toContain('ScheduleEntry #4');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(html).not.toContain('<img src=x onerror=alert(1)>');
  });

  it('renders failed run as an accessible error state', () => {
    const failed = { ...runView('unused'), status: 'failed', error: 'admin agent execution failed', result: null };
    const html = renderToStaticMarkup(<AssistantBrief run={failed} />);
    expect(html).toContain('role="alert"');
    expect(html).toContain('admin agent execution failed');
  });
});
