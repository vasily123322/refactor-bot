import type { CSSProperties, ReactNode } from 'react';

export function InlineStatus({
  children,
  className = 'inline-status',
}: {
  children: ReactNode;
  className?: string;
}) {
  if (!children) return null;

  return (
    <div className={className} role="status" aria-live="polite" aria-atomic="true">
      {children}
    </div>
  );
}

export function SkeletonBlock({
  height,
  width = '100%',
  radius = 8,
  className = '',
}: {
  height: number | string;
  width?: number | string;
  radius?: number | string;
  className?: string;
}) {
  const style: CSSProperties = {
    height,
    width,
    borderRadius: radius,
  };

  return <div className={`skeleton-block ${className}`.trim()} style={style} aria-hidden="true" />;
}

export function AsyncRegion({
  loading,
  empty,
  error = false,
  loadingLabel,
  loadingFallback,
  emptyFallback,
  errorFallback = null,
  children,
  className,
}: {
  loading: boolean;
  empty: boolean;
  error?: boolean;
  loadingLabel: string;
  loadingFallback: ReactNode;
  emptyFallback: ReactNode;
  errorFallback?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={className} aria-busy={loading || undefined}>
      {loading ? (
        <>
          <InlineStatus className="sr-only">{loadingLabel}</InlineStatus>
          <div className="async-region-skeleton" aria-hidden="true">
            {loadingFallback}
          </div>
        </>
      ) : error ? (
        errorFallback
      ) : empty ? (
        emptyFallback
      ) : (
        children
      )}
    </div>
  );
}
