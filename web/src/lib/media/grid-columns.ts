import { useMatches } from "@mantine/core";
import { useUIStore } from "@/stores/ui";

/**
 * 网格列数的响应式基准.
 *
 * sm/md 收窄一列: 48em 起导航栏展开, 内容区反而变窄, 沿用桌面列数会让缩略图小一档.
 * 海报墙与演员头像墙共用这一份, 不要各写一套断点.
 */
export const GRID_BREAKPOINT_COLS = { base: 2, xs: 3, sm: 3, md: 4, lg: 6, xl: 7 } as const;

export const MIN_GRID_COLUMNS = 3;
export const MAX_GRID_COLUMNS = 7;
export const DEFAULT_GRID_COLUMNS = 5;

/** 持久化值未经校验: 越界或 NaN 一律退回默认. */
export function clampGridColumns(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_GRID_COLUMNS;
  const rounded = Math.round(value);
  if (rounded < MIN_GRID_COLUMNS) return MIN_GRID_COLUMNS;
  if (rounded > MAX_GRID_COLUMNS) return MAX_GRID_COLUMNS;
  return rounded;
}

/**
 * 用户设定的列数是上限: 窄屏仍受断点约束, 否则小视口上的卡片会被压成不可读的缩略图;
 * 宽屏断点 (lg=6 / xl=7) 不小于该区间的两端, 宽屏因此完全跟随设定.
 */
export function useGridColumns(): number {
  const preferred = useUIStore((s) => s.gridColumns);
  const breakpointCols = useMatches(GRID_BREAKPOINT_COLS);
  return Math.min(preferred, breakpointCols);
}
