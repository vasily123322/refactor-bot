import type { SourceWorkerHealthView, SourceWorkerTickView } from './api';

export function sourceWorkerTickErrors(tick: SourceWorkerTickView): number {
  return (
    tick.lease_errors +
    tick.failures +
    tick.timeouts +
    tick.ingestion_errors +
    tick.unexpected_errors
  );
}

export function sourceWorkerTotalErrors(health: SourceWorkerHealthView): number {
  return (
    health.totals.lease_errors +
    health.totals.failures +
    health.totals.timeouts +
    health.totals.ingestion_errors +
    health.totals.unexpected_errors
  );
}

export function recentSourceWorkerTicks(
  health: SourceWorkerHealthView,
  limit = 8,
): SourceWorkerTickView[] {
  const boundedLimit = Math.max(1, Math.min(Math.trunc(limit), 20));
  return health.history.slice(-boundedLimit).reverse();
}
