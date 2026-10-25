/**
 * 列表页的导航记忆.
 *
 * 列表态的唯一事实来源仍是 URL search; 这里只额外记住"上次离开时是什么", 供侧栏入口回来时还原, 因此
 * 存 `sessionStorage` 而不是 `localStorage` — 它描述这一次浏览, 不是长期偏好. 长期偏好是侧栏的
 * 「钉默认」(`lib/nav-defaults.ts`), 两者互不替代: 钉默认决定入口地址, 这里决定"回来时像不像刚才".
 *
 * 关掉还原由 `interface.restore_list_state` 决定, 开关本身在设置页.
 */

const PREFIX = "amane:list-state:";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function rememberListSearch(key: string, search: Record<string, unknown>): void {
  try {
    sessionStorage.setItem(`${PREFIX}${key}`, JSON.stringify(search));
  } catch {
    // 隐私模式或配额满: 记不住就不记, 不该影响页面本身.
  }
}

export function recalledListSearch(key: string): Record<string, unknown> | undefined {
  try {
    const raw = sessionStorage.getItem(`${PREFIX}${key}`);
    if (raw == null || raw === "") return undefined;
    const parsed: unknown = JSON.parse(raw);
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

/** 列表是否处于"干净"状态: 没有任何筛选与检索词, 视图与页码也都在默认值上. */
export function isCleanListSearch(
  search: Record<string, unknown>,
  defaults: readonly string[] = ["view", "page"],
): boolean {
  return Object.entries(search).every(
    ([key, value]) => value == null || value === "" || defaults.includes(key),
  );
}
