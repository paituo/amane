/**
 * 影片收藏.
 *
 * 影片没有独立的收藏列 (`is_favorite` 只存在于分类实体), 因此收藏复用用户标签: 标签「★收藏」的
 * 成员就是已收藏的影片. 收藏页因此就是 `/meta?user_tag_id=<该标签 id>`, 不需要专用路由或表.
 *
 * 标签名是数据 (`user_tags.name`), 不随界面语言变化 —— 否则切换语言后收藏会指向另一个标签.
 *
 * 列表项不带标签信息, 收藏态只能靠一次「该标签的成员 id 集合」查询判定; 集合走 React Query 缓存,
 * 收藏动作后整族失效.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { batchMetadataUserTags, createUserTags, listFacets, listMetadata } from "@/client/sdk.gen";

/** 收藏标签名. 用户可在合集页改名, 改名后即视为另一个收藏集合. */
export const FAVORITE_TAG_NAME = "★收藏";

/** 与 `GET /metadata` 的 limit 上限一致. */
const PAGE_SIZE = 200;

/** 收藏集合一次取全量; 缓存期内不重复翻页. */
const FAVORITE_STALE_MS = 60_000;

const EMPTY_FAVORITE_IDS: ReadonlySet<number> = new Set();

export const favoriteTagQueryKey = ["favorite", "tag"] as const;

function favoriteIdsQueryKey(tagId: number | null) {
  return ["favorite", "ids", tagId] as const;
}

async function findFavoriteTagId(): Promise<number | null> {
  const { data } = await listFacets({
    path: { kind: "user_tag" },
    query: { search: FAVORITE_TAG_NAME, limit: 50, offset: 0 },
    throwOnError: true,
  });
  return data.items.find((item) => item.name === FAVORITE_TAG_NAME)?.id ?? null;
}

/** 取回或新建收藏标签 (端点按名称幂等, 并发同名由后端收敛). */
export async function ensureFavoriteTagId(): Promise<number> {
  const { data } = await createUserTags({
    body: { names: [FAVORITE_TAG_NAME] },
    throwOnError: true,
  });
  return data.items[0].id;
}

/** 只读: 还没收藏过任何影片时返回 null, 不建标签 (侧栏入口不许副作用). */
export function useFavoriteTagId() {
  return useQuery({
    queryKey: favoriteTagQueryKey,
    queryFn: findFavoriteTagId,
    staleTime: FAVORITE_STALE_MS,
  });
}

async function fetchFavoriteIds(tagId: number): Promise<number[]> {
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

/** 已收录的影片 id 集合; 标签不存在或查询失败时是空集合. */
export function useFavoriteIds(): ReadonlySet<number> {
  const tagId = useFavoriteTagId().data ?? null;
  const { data } = useQuery({
    queryKey: favoriteIdsQueryKey(tagId),
    queryFn: () => fetchFavoriteIds(tagId ?? 0),
    enabled: tagId != null,
    staleTime: FAVORITE_STALE_MS,
  });
  // 按缓存数组引用记忆: 每张卡片各订阅一次, 每次渲染新建 Set 会让整面海报墙重渲染.
  return useMemo(() => (data ? new Set(data) : EMPTY_FAVORITE_IDS), [data]);
}

/** 切换单部影片的收藏态; 两次动作都是整体赋值, 界面不做二次确认. */
export function useFavoriteMutation() {
  const queryClient = useQueryClient();
  const knownTagId = useFavoriteTagId().data ?? null;
  return useMutation({
    mutationFn: async ({ metadataId, favorite }: { metadataId: number; favorite: boolean }) => {
      // 标签已取到时不再重复请求「取回或新建」, 只发一次挂载.
      const tagId = knownTagId ?? (await ensureFavoriteTagId());
      await batchMetadataUserTags({
        body: {
          ids: [metadataId],
          user_tag_ids: [tagId],
          action: favorite ? "attach" : "detach",
        },
        throwOnError: true,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: favoriteTagQueryKey });
      // 前缀失效: 普通列表与无限列表的 _id 都是 listMetadata, 收藏页因此同步刷新.
      void queryClient.invalidateQueries({ queryKey: [{ _id: "listMetadata" }] });
      void queryClient.invalidateQueries({ queryKey: [{ _id: "listFacets" }] });
      void queryClient.invalidateQueries({ queryKey: [{ _id: "getMetadata" }] });
    },
  });
}

/** 详情页判据: 该条目的用户标签里是否有收藏标签. */
export function hasFavoriteTag(
  userTags: ReadonlyArray<{ id: number }>,
  favoriteTagId: number | null,
): boolean {
  if (favoriteTagId == null) return false;
  return userTags.some((tag) => tag.id === favoriteTagId);
}
