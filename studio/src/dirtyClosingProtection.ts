export type DirtyClosingProtectionBridge = Readonly<{
  enable(): boolean;
  dispose(): boolean;
}>;

export function createDirtyClosingProtectionController(
  bridge: DirtyClosingProtectionBridge,
) {
  let ownsClosingProtection = false;

  const release = (): boolean => {
    if (!ownsClosingProtection) return true;
    const released = bridge.dispose();
    if (released) ownsClosingProtection = false;
    return released;
  };

  return {
    sync(dirty: boolean): boolean {
      if (!dirty) return release();
      if (ownsClosingProtection) return true;
      const enabled = bridge.enable();
      if (enabled) ownsClosingProtection = true;
      return enabled;
    },
    dispose: release,
    ownsProtection(): boolean {
      return ownsClosingProtection;
    },
  };
}
