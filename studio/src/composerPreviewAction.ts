export type ComposerPreviewInFlight = {
  current: Promise<void> | null;
};

export function runComposerPreviewOnce(
  inFlight: ComposerPreviewInFlight,
  operation: () => Promise<void>,
): Promise<void> {
  const current = inFlight.current;
  if (current) return current;

  const pending = operation();
  inFlight.current = pending;
  const clear = () => {
    if (inFlight.current === pending) inFlight.current = null;
  };
  pending.then(clear, clear);
  return pending;
}
