/**
 * 把列表行 / 列表项转成待播放队列的条目快照.
 *
 * 两处来源字段名不同 (`MetadataResponse` 有 title, `MediaFileResponse` 只有番号), 但队列只认
 * `metadataId`, 因此统一在这里做一次映射, 调用方不必各写一遍.
 */

import type { MediaFileResponse, MetadataResponse } from "@/client/types.gen";
import type { PlaylistEntry } from "@/stores/playlist";

export function entryFromMetadata(item: MetadataResponse): PlaylistEntry {
  return {
    metadataId: item.id,
    number: item.number,
    title: item.title ?? null,
    posterUrl: item.poster_url ?? item.poster_urls?.[0] ?? null,
  };
}

/** 未绑定条目的文件没有可播对象, 返回 null. */
export function entryFromMediaFile(item: MediaFileResponse): PlaylistEntry | null {
  if (item.metadata_id == null) return null;
  return {
    metadataId: item.metadata_id,
    number: item.number ?? "",
    title: null,
    posterUrl: item.poster_url ?? null,
  };
}
