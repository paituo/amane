/**
 * 影片黑名单 — 实现机制与收藏 (favorites.ts) 一致: 复用用户标签.
 * 标签「⛔黑名单」的成员就是黑名单影片; 标签名是数据 (`user_tags.name`),
 * 不随界面语言变化 —— 否则切换语言后黑名单会指向另一个标签.
 *
 * 列表项不带标签信息, 黑名单态只能靠一次「该标签的成员 id 集合」查询判定;
 * 集合走 React Query 缓存, 挂载动作后整族失效.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { batchMetadataUserTags, createUserTags, listFacets, listMetadata } from "@/client/sdk.gen";

/** 黑名单标签名. */
export const BLACKLIST_TAG_NAME = "⛔黑名单";

/** 与 `GET /metadata` 的 limit 上限一致. */
const PAGE_SIZE = 200;

/** 黑名单集合一次取全量; 缓存期内不重复翻页. */
const BLACKLIST_STALE_MS = 60_000;

const EMPTY_BLACKLIST_IDS: ReadonlySet<number> = new Set();

export const blacklistTagQueryKey = ["blacklist", "tag"] as const;

function blacklistIdsQueryKey(tagId: number | null) {
  return ["blacklist", "ids", tagId] as const;
}

async function findBlacklistTagId(): Promise<number | null> {
  const { data } = await listFacets({
    path: { kind: "user_tag" },
    query: { search: BLACKLIST_TAG_NAME, limit: 50, offset: 0 },
    throwOnError: true,
  });
  return data.items.find((item) => item.name === BLACKLIST_TAG_NAME)?.id ?? null;
}

/** 取回或新建黑名单标签 (端点按名称幂等, 并发同名由后端收敛). */
export async function ensureBlacklistTagId(): Promise<number> {
  const { data } = await createUserTags({
    body: { names: [BLACKLIST_TAG_NAME] },
    throwOnError: true,
  });
  return data.items[0].id;
}

/** 只读: 还没有影片进过黑名单时返回 null, 不建标签 (侧栏入口不许副作用). */
export function useBlacklistTagId() {
  return useQuery({
    queryKey: blacklistTagQueryKey,
    queryFn: findBlacklistTagId,
    staleTime: BLACKLIST_STALE_MS,
  });
}

async function fetchBlacklistIds(tagId: number): Promise<number[]> {
  const ids: number[] = [];
  let offset = 0;
  for (;;) {
    const { data } = await listMetadata({
      query: { user_tag_id: [tagId], limit: PAGE_SIZE, offset },
      throwOnError: true,
    });
    for (const item of data.items) ids.push(item.id);
    offset += data.items.length;
    if (data.items.length === 0 || offset >= data.total) return ids;
  }
}

/** 黑名单影片 id 集合; 标签不存在或查询失败时是空集合. */
export function useBlacklistIds(): ReadonlySet<number> {
  const tagId = useBlacklistTagId().data ?? null;
  const { data } = useQuery({
    queryKey: blacklistIdsQueryKey(tagId),
    queryFn: () => fetchBlacklistIds(tagId ?? 0),
    enabled: tagId != null,
    staleTime: BLACKLIST_STALE_MS,
  });
  // 按缓存数组引用记忆: 每张卡片各订阅一次, 每次渲染新建 Set 会让整面海报墙重渲染.
  return useMemo(() => (data ? new Set(data) : EMPTY_BLACKLIST_IDS), [data]);
}

/** 切换单部影片的黑名单态; 两次动作都是整体赋值, 界面不做二次确认. */
export function useBlacklistMutation() {
  const queryClient = useQueryClient();
  const knownTagId = useBlacklistTagId().data ?? null;
  return useMutation({
    mutationFn: async ({
      metadataId,
      blacklisted,
    }: {
      metadataId: number;
      blacklisted: boolean;
    }) => {
      // 标签已取到时不再重复请求「取回或新建」, 只发一次挂载.
      const tagId = knownTagId ?? (await ensureBlacklistTagId());
      await batchMetadataUserTags({
        body: {
          ids: [metadataId],
          user_tag_ids: [tagId],
          action: blacklisted ? "attach" : "detach",
        },
        throwOnError: true,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: blacklistTagQueryKey });
      // 前缀失效: 普通列表与无限列表的 _id 都是 listMetadata, 黑名单页因此同步刷新.
      void queryClient.invalidateQueries({ queryKey: [{ _id: "listMetadata" }] });
      void queryClient.invalidateQueries({ queryKey: [{ _id: "listFacets" }] });
      void queryClient.invalidateQueries({ queryKey: [{ _id: "getMetadata" }] });
    },
  });
}
