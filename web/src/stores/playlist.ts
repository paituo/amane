/**
 * 待播放队列.
 *
 * 队列跨页面存在 (海报墙、片库列表、详情页都往里加), 因此不进页面状态, 只存浏览器本地. 存入的是加入
 * 那一刻的条目快照 — 番号、标题、封面 — 队列要在没有网络、条目被删之后仍能显示, 不能靠再查一次列表.
 * 播放入口只认 `metadataId`: 路径由后端按条目从索引里取.
 *
 * 加入是**追加**: 已有条目保持原顺序, 重复加入只忽略重复项, 不清空队列.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface PlaylistEntry {
  metadataId: number;
  number: string;
  title: string | null;
  posterUrl: string | null;
}

interface PlaylistState {
  items: PlaylistEntry[];
  /** 追加条目: 已存在的不重复加入, 顺序按传入顺序接在末尾. */
  append: (entries: PlaylistEntry[]) => void;
  remove: (metadataIds: readonly number[]) => void;
  clear: () => void;
  /** 把选中的条目整体上移/下移一格; 与未选中的相邻条目交换位置. */
  move: (metadataIds: readonly number[], direction: -1 | 1) => void;
}

const STORAGE_KEY = "amane-playlist";

/** 队列里判重与排序都按条目 id, 与条目自身字段无关. */
function withoutExisting(items: PlaylistEntry[], incoming: PlaylistEntry[]): PlaylistEntry[] {
  const seen = new Set(items.map((item) => item.metadataId));
  const added: PlaylistEntry[] = [];
  for (const entry of incoming) {
    if (seen.has(entry.metadataId)) continue;
    seen.add(entry.metadataId);
    added.push(entry);
  }
  return added;
}

/**
 * 选中项整体位移一格.
 *
 * 逐项向后遍历 (下移) 或向前遍历 (上移), 保证同一块内的相邻项不会互相挡住: 只有"越过的那一格不是
 * 选中项"时才交换.
 */
function moveSelected(
  items: PlaylistEntry[],
  selected: ReadonlySet<number>,
  direction: -1 | 1,
): PlaylistEntry[] {
  const next = [...items];
  const order = direction === 1 ? [...next.keys()].toReversed() : [...next.keys()];
  for (const index of order) {
    if (!selected.has(next[index].metadataId)) continue;
    const target = index + direction;
    if (target < 0 || target >= next.length) continue;
    if (selected.has(next[target].metadataId)) continue;
    [next[index], next[target]] = [next[target], next[index]];
  }
  return next;
}

export const usePlaylistStore = create<PlaylistState>()(
  persist(
    (set) => ({
      items: [],
      append: (entries) =>
        set((state) => {
          const added = withoutExisting(state.items, entries);
          return added.length === 0 ? state : { items: [...state.items, ...added] };
        }),
      remove: (metadataIds) =>
        set((state) => {
          const drop = new Set(metadataIds);
          return { items: state.items.filter((item) => !drop.has(item.metadataId)) };
        }),
      clear: () => set({ items: [] }),
      move: (metadataIds, direction) =>
        set((state) => ({
          items: moveSelected(state.items, new Set(metadataIds), direction),
        })),
    }),
    {
      name: STORAGE_KEY,
      // 只持久化队列内容; 动作函数由 zustand 每次重建.
      partialize: (state) => ({ items: state.items }),
      merge: (persisted, current) => {
        const raw = (persisted as { items?: unknown } | undefined)?.items;
        if (!Array.isArray(raw)) return current;
        const items = raw.filter((item): item is PlaylistEntry => {
          return (
            typeof item === "object" &&
            item != null &&
            typeof (item as PlaylistEntry).metadataId === "number"
          );
        });
        return { ...current, items };
      },
    },
  ),
);
