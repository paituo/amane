/**
 * 本机播放: 把已入库的文件交给服务所在机器的外部播放器.
 *
 * 用不用外部播放器由设置页的「播放器」决定 (`playback.external` / `mode` / `player`). 详情页的封面按钮
 * 与播放卡片共用这里的查询, react-query 按 key 去重, 一页只发一次请求.
 *
 * 前端只负责"请求打开": 路径由后端从条目索引里取, 不接受前端传入, 否则这个接口等于一个任意程序启动器.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  getLocalPlayerOptions,
  openLocalPlaybackMutation,
} from "@/client/@tanstack/react-query.gen";
import type { PlaybackLocalPlayerResponse } from "@/client/types.gen";

/** 本机播放设置. 改动只发生在设置页, 缓存期内不必重取. */
export function useLocalPlayer() {
  return useQuery({ ...getLocalPlayerOptions(), staleTime: 60_000 });
}

/** 请求用外部播放器打开某个条目. */
export function useOpenLocalPlayback() {
  return useMutation(openLocalPlaybackMutation());
}

/**
 * 当前设置能不能真的拉起播放器: 开关打开, 且走系统关联或已填自定义路径.
 *
 * `mode=custom` 但路径为空时后端会拒绝, 前端据此提前显示"尚未配置", 而不是给出一个点了会报错的按钮.
 */
export function canLaunchLocalPlayer(state: PlaybackLocalPlayerResponse | undefined): boolean {
  if (state?.enabled !== true) {
    return false;
  }
  return state.mode === "system" || state.player != null;
}
