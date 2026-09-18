export type ChannelLoadPhase = 'loading' | 'loaded' | 'error-without-valid-data';

export type ChannelLoadState = {
  channelId: number;
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

export type ChannelRequestToken = Readonly<{
  channelId: number;
  epoch: number;
}>;

export class ChannelRequestOwnership {
  private epoch = 0;

  begin(channelId: number): ChannelRequestToken {
    this.epoch += 1;
    return { channelId, epoch: this.epoch };
  }

  invalidate(): void {
    this.epoch += 1;
  }

  isCurrent(token: ChannelRequestToken, currentChannelId: number | null): boolean {
    return token.epoch === this.epoch && token.channelId === currentChannelId;
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
