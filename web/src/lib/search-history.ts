/**
 * 最近搜索词.
 *
 * 按页面 (列表入口) 分开记: 片库搜过的番号不该出现在演员页的下拉里. 去重后新的排最前, 只留最近
 * `SEARCH_HISTORY_LIMIT` 条 — 下拉是个快捷入口, 不是搜索日志.
 */

export const SEARCH_HISTORY_LIMIT = 10;

export function pushSearchTerm(terms: readonly string[], term: string): string[] {
  const trimmed = term.trim();
  if (trimmed === "") return [...terms];
  return [trimmed, ...terms.filter((item) => item !== trimmed)].slice(0, SEARCH_HISTORY_LIMIT);
}

export function dropSearchTerm(terms: readonly string[], term: string): string[] {
  return terms.filter((item) => item !== term);
}
