export interface WorkspaceLayoutConfig {
  chatMinWidth: number;
  consoleMinWidth: number;
  consoleDefaultWidth: number;
  consoleMaxWidth: number;
  leftMinWidth: number;
  leftDefaultWidth: number;
  leftMaxWidth: number;
}

export const DEFAULT_WORKSPACE_LAYOUT: WorkspaceLayoutConfig = {
  chatMinWidth: 420,
  consoleMinWidth: 300,
  consoleDefaultWidth: 340,
  consoleMaxWidth: 520,
  leftMinWidth: 208,
  leftDefaultWidth: 238,
  leftMaxWidth: 320,
};

export interface WorkspaceLayoutInput {
  containerWidth: number;
  leftWidth: number;
  consoleWidth: number;
  config?: Partial<WorkspaceLayoutConfig>;
}

export interface WorkspaceLayout {
  containerWidth: number;
  leftWidth: number;
  consoleWidth: number;
  consoleMaxWidth: number;
  canDockConsole: boolean;
  isNarrow: boolean;
}

export type EscapeAction = "close-preview" | "close-artifact" | "close-drawer" | "none";

function configFor(overrides?: Partial<WorkspaceLayoutConfig>): WorkspaceLayoutConfig {
  return { ...DEFAULT_WORKSPACE_LAYOUT, ...overrides };
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
}

export function clampLeftWidth(
  width: number,
  configOverrides?: Partial<WorkspaceLayoutConfig>,
): number {
  const config = configFor(configOverrides);
  return clamp(width, config.leftMinWidth, config.leftMaxWidth);
}

export function deriveConsoleMaxWidth(
  containerWidth: number,
  leftWidth: number,
  configOverrides?: Partial<WorkspaceLayoutConfig>,
): number {
  const config = configFor(configOverrides);
  const available = containerWidth - leftWidth - config.chatMinWidth;
  return clamp(available, config.consoleMinWidth, config.consoleMaxWidth);
}

export function clampConsoleWidth(
  width: number,
  containerWidth: number,
  leftWidth: number,
  configOverrides?: Partial<WorkspaceLayoutConfig>,
): number {
  const config = configFor(configOverrides);
  return clamp(width, config.consoleMinWidth, deriveConsoleMaxWidth(containerWidth, leftWidth, config));
}

export function canDockConsole(
  containerWidth: number,
  leftWidth: number,
  consoleWidth: number,
  configOverrides?: Partial<WorkspaceLayoutConfig>,
): boolean {
  const config = configFor(configOverrides);
  return containerWidth >= leftWidth + config.chatMinWidth + consoleWidth;
}

export function resolveWorkspaceLayout(input: WorkspaceLayoutInput): WorkspaceLayout {
  const config = configFor(input.config);
  const containerWidth = Math.max(0, input.containerWidth);
  const leftWidth = clampLeftWidth(input.leftWidth, config);
  const consoleMaxWidth = deriveConsoleMaxWidth(containerWidth, leftWidth, config);
  const consoleWidth = clampConsoleWidth(input.consoleWidth, containerWidth, leftWidth, config);
  const dockable = canDockConsole(containerWidth, leftWidth, consoleWidth, config);
  return {
    containerWidth,
    leftWidth,
    consoleWidth,
    consoleMaxWidth,
    canDockConsole: dockable,
    isNarrow: !dockable,
  };
}

export function nextEscapeAction(state: {
  previewOpen: boolean;
  artifactPageOpen: boolean;
  drawerOpen: boolean;
}): EscapeAction {
  if (state.previewOpen) return "close-preview";
  if (state.artifactPageOpen) return "close-artifact";
  if (state.drawerOpen) return "close-drawer";
  return "none";
}
