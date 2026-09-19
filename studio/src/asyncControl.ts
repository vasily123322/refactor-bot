export type ChannelLoadPhase = 'loading' | 'loaded' | 'error-without-valid-data';

export type ChannelLoadState = {
  channelId: number;
  phase: ChannelLoadPhase;
};

export type ScopedLoadState = {
  channelId: number;
  scopeKey: string;
  phase: ChannelLoadPhase;
};

export type ChannelDataView =
  | 'loading'
  | 'loaded-empty'
  | 'loaded-data'
  | 'error-without-valid-data';

export function resolveChannelDataView(
  state: ChannelLoadState | null,
  channelId: number,
  itemCount: number,
): ChannelDataView {
  if (!state || state.channelId !== channelId || state.phase === 'loading') return 'loading';
  if (state.phase === 'error-without-valid-data') return 'error-without-valid-data';
  return itemCount === 0 ? 'loaded-empty' : 'loaded-data';
}

export function resolveScopedDataView(
  state: ScopedLoadState | null,
  channelId: number,
  scopeKey: string,
  itemCount: number,
): ChannelDataView {
  if (
    !state
    || state.channelId !== channelId
    || state.scopeKey !== scopeKey
    || state.phase === 'loading'
  ) {
    return 'loading';
  }
  if (state.phase === 'error-without-valid-data') return 'error-without-valid-data';
  return itemCount === 0 ? 'loaded-empty' : 'loaded-data';
}

export type ChannelRequestToken = Readonly<{
  channelId: number;
  scopeKey: string | null;
  epoch: number;
}>;

export class ChannelRequestOwnership {
  private epoch = 0;

  begin(channelId: number, scopeKey: string | null = null): ChannelRequestToken {
    this.epoch += 1;
    return { channelId, scopeKey, epoch: this.epoch };
  }

  invalidate(): void {
    this.epoch += 1;
  }

  isCurrent(
    token: ChannelRequestToken,
    currentChannelId: number | null,
    currentScopeKey: string | null = token.scopeKey,
  ): boolean {
    return (
      token.epoch === this.epoch
      && token.channelId === currentChannelId
      && token.scopeKey === currentScopeKey
    );
  }
}

export type ExclusiveOperationToken = Readonly<{
  key: string;
  id: symbol;
}>;

export class ExclusiveOperationLock {
  private holder: ExclusiveOperationToken | null = null;

  tryAcquire(key: string): ExclusiveOperationToken | null {
    if (this.holder) return null;
    const token = { key, id: Symbol(key) };
    this.holder = token;
    return token;
  }

  release(token: ExclusiveOperationToken): boolean {
    if (!this.holder || this.holder.id !== token.id) return false;
    this.holder = null;
    return true;
  }

  isLocked(): boolean {
    return this.holder !== null;
  }

  activeKey(): string | null {
    return this.holder?.key ?? null;
  }
}

export type ScopedExclusiveOperationToken = Readonly<{
  scope: string | null;
  key: string;
  id: symbol;
}>;

export class ScopedExclusiveOperationLock {
  private globalHolder: ScopedExclusiveOperationToken | null = null;
  private scopedHolders = new Map<string, ScopedExclusiveOperationToken>();

  tryAcquire(scope: string | null, key: string): ScopedExclusiveOperationToken | null {
    if (scope === null) {
      if (this.globalHolder || this.scopedHolders.size > 0) return null;
      const token = { scope, key, id: Symbol(key) };
      this.globalHolder = token;
      return token;
    }

    if (this.globalHolder || this.scopedHolders.has(scope)) return null;
    const token = { scope, key, id: Symbol(key) };
    this.scopedHolders.set(scope, token);
    return token;
  }

  release(token: ScopedExclusiveOperationToken): boolean {
    if (token.scope === null) {
      if (!this.globalHolder || this.globalHolder.id !== token.id) return false;
      this.globalHolder = null;
      return true;
    }

    const holder = this.scopedHolders.get(token.scope);
    if (!holder || holder.id !== token.id) return false;
    this.scopedHolders.delete(token.scope);
    return true;
  }
}

export async function runScopedExclusiveOperation<T>(
  lock: ScopedExclusiveOperationLock,
  scope: string | null,
  key: string,
  operation: () => Promise<T>,
  onActiveChange?: (key: string, active: boolean) => void,
): Promise<{ started: boolean; value?: T }> {
  const token = lock.tryAcquire(scope, key);
  if (!token) return { started: false };

  onActiveChange?.(key, true);
  try {
    return { started: true, value: await operation() };
  } finally {
    if (lock.release(token)) onActiveChange?.(key, false);
  }
}

export async function runExclusiveOperation<T>(
  lock: ExclusiveOperationLock,
  key: string,
  operation: () => Promise<T>,
  onActiveKeyChange?: (key: string | null) => void,
): Promise<{ started: boolean; value?: T }> {
  const token = lock.tryAcquire(key);
  if (!token) return { started: false };

  onActiveKeyChange?.(key);
  try {
    return { started: true, value: await operation() };
  } finally {
    if (lock.release(token)) onActiveKeyChange?.(null);
  }
}
