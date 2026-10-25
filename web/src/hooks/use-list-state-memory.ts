import { useEffect } from "react";
import { useConfig } from "@/hooks/use-config";
import { isCleanListSearch, recalledListSearch, rememberListSearch } from "@/lib/list-state";

/**
 * 列表页记住自己当前的 search.
 *
 * 页面本身仍以 URL 为准, 这里只是把"离开时是什么"抄一份到 `sessionStorage`, 供侧栏入口回来时还原.
 * 依赖用序列化后的字符串: 路由每次渲染都可能给出新的对象实例, 按引用比较会每次都写一次.
 *
 * `enabled` 用来排除"同路径但不同入口"的情况: 收藏与片库共用 `/meta`, 收藏是一套固定筛选,
 * 记进片库的导航记忆会在下次点「片库」时把收藏筛选带回来.
 */
export function useListStateMemory(
  key: string,
  search: Record<string, unknown>,
  { enabled = true }: { enabled?: boolean } = {},
): void {
  const serialized = JSON.stringify(search);
  useEffect(() => {
    if (!enabled) return;
    rememberListSearch(key, JSON.parse(serialized) as Record<string, unknown>);
  }, [key, serialized, enabled]);
}

/**
 * 侧栏入口该跳到哪儿: 还原上次的 search, 还是用钉住的默认.
 *
 * 开关是服务端配置 `interface.restore_list_state` (默认开). 关掉时行为回到"侧栏入口永远是干净列表",
 * 与钉默认 (`lib/nav-defaults.ts`) 的语义一致 —— 钉默认决定入口地址, 这里只决定"回来时像不像刚才".
 *
 * 上次的 search 若本身是干净的 (没有搜索词与筛选), 就没必要还原: 直接给默认, 免得把上一个库/页码带回来.
 *
 * 返回的是"待跳转的 search", 不是已校验的 search: 存下来的是上一次路由解析后的对象, 而目标路由会再跑
 * 一遍 `validateSearch`, 非法项在那里被丢弃或兜底, 因此这里按调用方的 schema 类型断言.
 */
export function useListStateRestore(): <T extends Record<string, unknown>>(
  key: string,
  fallback: T,
) => T {
  const { data } = useConfig();
  // 配置还没取回时按默认值处理: 默认是开, 与后端一致.
  const enabled = data?.interface?.restore_list_state ?? true;

  return (key, fallback) => {
    if (!enabled) return fallback;
    const recalled = recalledListSearch(key);
    if (recalled == null || isCleanListSearch(recalled)) return fallback;
    return recalled as typeof fallback;
  };
}
