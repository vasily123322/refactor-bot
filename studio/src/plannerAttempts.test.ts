import { describe, expect, it } from 'vitest';

import { plannerAttemptLabel } from './plannerAttempts';

describe('plannerAttemptLabel', () => {
  it('describes an active sending attempt', () => {
    expect(plannerAttemptLabel({ attempt_number: 2, attempt_status: 'sending' }))
      .toBe('Попытка #2 · выполняется');
  });

  it('describes a failed attempt without transport details', () => {
    expect(plannerAttemptLabel({ attempt_number: 1, attempt_status: 'failed' }))
      .toBe('Попытка #1 · ошибка');
  });

  it('hides missing attempt state', () => {
    expect(plannerAttemptLabel({ attempt_number: null, attempt_status: null })).toBeNull();
  });
});
