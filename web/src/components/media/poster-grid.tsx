import { AspectRatio, Button, Center, SimpleGrid, Skeleton, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconBan,
  IconPlaylistAdd,
  IconPlayerPlay,
  IconRefresh,
  IconSelectAll,
  IconSquareOff,
  IconStar,
  IconTrash,
} from "@tabler/icons-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  batchDeleteMetadataMutation,
  batchScrapeMetadataMutation,
  listMetadataQueryKey,
  openLocalPlaylistMutation,
} from "@/client/@tanstack/react-query.gen";
import { batchMetadataUserTags } from "@/client/sdk.gen";
import type { MetadataResponse } from "@/client/types.gen";
import { SelectionBar } from "@/components/common/selection-bar";
import { HintedActionIcon } from "@/components/common/hinted-action-icon";
import { MetadataViewer } from "@/components/media/metadata-viewer";
import { PlaybackModal } from "@/components/media/playback-modal";
import { PlaybackQueueModal } from "@/components/media/playback-queue-modal";
import { useAppMutation } from "@/hooks/use-app-mutation";
import { useIdSelection } from "@/hooks/use-id-selection";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { blacklistTagQueryKey, ensureBlacklistTagId } from "@/lib/media/blacklist";
import { ensureFavoriteTagId, favoriteTagQueryKey } from "@/lib/media/favorites";
import { useGridColumns } from "@/lib/media/grid-columns";
import { useLocalPlayer, useOpenLocalPlayback } from "@/lib/media/local-player";
import { entryFromMetadata } from "@/lib/media/playlist-entries";
import { type PlaylistEntry, usePlaylistStore } from "@/stores/playlist";
import { PosterCard } from "./poster-card";

interface PosterGridProps {
  items: MetadataResponse[];
  loading?: boolean;
  emptyMessage?: string;
  /** 传入时海报显示演员出演时年龄. */
  actorBirthday?: string | null;
  /** 还有下一页时由弹窗在接近末尾处调用, 继续往下翻. */
  onFetchMore?: () => void;
}

/**
 * 海报墙 + 就地详情 + 多选批量操作.
 *
 * 点击卡片不是导航而是原地打开弹窗 (方向键与两侧箭头翻阅已加载的序列), 但链接仍旧保留:
 * 中键与修饰键沿用浏览器的新标签行为, 那种方式打开的是详情页面.
 * 卡片左上角复选框做多选, 批量动作 (刮削/收藏/黑名单/删除) 收在网格上方的工具条里,
 * 与列表视图 (MetaTable) 的批量操作保持同一套语义.
 */
export function PosterGrid({
  items,
  loading = false,
  emptyMessage,
  actorBirthday,
  onFetchMore,
}: PosterGridProps) {
  const { t } = useTranslation(["metadata", "common"]);
  const cols = useGridColumns();
  const queryClient = useQueryClient();
  // 按 id 记录打开的那一部: 追加分页或重新获取之后, 位置仍然对得上.
  const [openId, setOpenId] = useState<number | null>(null);
  // 播放弹窗; 面板按 metadataId 自行加载播放源与码流.
  const [playId, setPlayId] = useState<number | null>(null);
  // 内嵌播放器的队列 (外部播放器未启用时, "播放选中项"落到这里).
  const [queue, setQueue] = useState<readonly PlaylistEntry[]>([]);

  const open = useCallback((id: number) => setOpenId(id), []);

  const { selected, selectedIds, toggleOne, selectAll, invert, clear } = useIdSelection();
  const [tagBusy, setTagBusy] = useState(false);

  // 海报墙播放按钮: 外部播放器开着就直接拉起本机播放器, 否则弹出内嵌播放器页面直接播放.
  const localPlayer = useLocalPlayer();
  const openLocal = useOpenLocalPlayback();
  const openPlaylist = useAppMutation({
    mutationOptions: openLocalPlaylistMutation(),
    successToast: (result) =>
      t("playlist.openedExternal", {
        count: result.paths.length,
        player: result.player ?? t("detail.openLocalSystem"),
      }),
  });
  const appendToPlaylist = usePlaylistStore((state) => state.append);

  /** 选中项的快照, 供播放与加入队列共用; 顺序照列表顺序. */
  const selectedEntries = items.filter((item) => selected.has(item.id)).map(entryFromMetadata);

  /** 播放选中项: 外部播放器开着就交整批, 否则打开内嵌队列. */
  function handlePlaySelected() {
    if (selectedEntries.length === 0) return;
    if (localPlayer.data?.enabled === true) {
      openPlaylist.mutate({
        body: { metadata_ids: selectedEntries.map((entry) => entry.metadataId) },
      });
      return;
    }
    setQueue(selectedEntries);
  }

  function handleAddToPlaylist() {
    if (selectedEntries.length === 0) return;
    const before = usePlaylistStore.getState().items.length;
    appendToPlaylist(selectedEntries);
    const added = usePlaylistStore.getState().items.length - before;
    notifications.show({
      message:
        added === 0 ? t("playlist.addedAllDuplicate") : t("playlist.added", { count: added }),
      color: added === 0 ? "gray" : "blue",
    });
  }

  function handlePlay(item: MetadataResponse) {
    if (localPlayer.data?.enabled === true) {
      openLocal.mutate(
        { path: { metadata_id: item.id } },
        {
          onError: (err) =>
            notifications.show({
              message: extractErrorMessage(err, t("common:toast.operationFailed")),
              color: "red",
            }),
        },
      );
    } else {
      setPlayId(item.id);
    }
  }

  const batchScrape = useMutation({
    ...batchScrapeMetadataMutation(),
    onSuccess: (res) => {
      notifications.show({
        message: t("common:toast.batchScrapeStarted", { count: res.submitted }),
        color: "blue",
      });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.batchScrapeFailed")),
        color: "red",
      }),
  });

  const batchDelete = useMutation({
    ...batchDeleteMetadataMutation(),
    onSuccess: () => {
      notifications.show({ message: t("common:toast.metadataDeleted"), color: "blue" });
      clear();
      void queryClient.invalidateQueries({ queryKey: listMetadataQueryKey() });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  function invalidateLists() {
    void queryClient.invalidateQueries({ queryKey: listMetadataQueryKey() });
    void queryClient.invalidateQueries({ queryKey: [{ _id: "listFacets" }] });
  }

  /** 批量挂标签 (收藏/黑名单共用): 标签幂等创建后整体 attach. */
  async function batchAttach(
    tagKey: readonly string[],
    ensureTagId: () => Promise<number>,
    doneMessage: string,
  ) {
    setTagBusy(true);
    try {
      const tagId = await ensureTagId();
      await batchMetadataUserTags({
        body: { ids: selectedIds, user_tag_ids: [tagId], action: "attach" },
        throwOnError: true,
      });
      notifications.show({ message: doneMessage, color: "blue" });
      void queryClient.invalidateQueries({ queryKey: tagKey });
      invalidateLists();
      clear();
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    } finally {
      setTagBusy(false);
    }
  }

  const batchFavorite = () =>
    batchAttach(
      favoriteTagQueryKey,
      ensureFavoriteTagId,
      t("favorite.batchDone", { count: selectedIds.length }),
    );

  const batchBlacklist = () =>
    batchAttach(
      blacklistTagQueryKey,
      ensureBlacklistTagId,
      t("blacklist.batchDone", { count: selectedIds.length }),
    );

  async function handleBatchDelete() {
    const ok = await confirm({
      title: t("actions.confirmBatchDeleteTitle"),
      message: t("actions.confirmBatchDeleteDesc", { count: selectedIds.length }),
      confirmLabel: t("common:actions.delete"),
    });
    if (!ok) return;
    batchDelete.mutate({ body: { ids: selectedIds } });
  }

  const busy = tagBusy || batchScrape.isPending || batchDelete.isPending;

  if (loading) {
    return (
      <SimpleGrid cols={cols} spacing="md">
        {Array.from({ length: 12 }, (_, i) => (
          <Stack key={i} gap={2}>
            <AspectRatio ratio={0.7}>
              <Skeleton radius="md" />
            </AspectRatio>
            <Skeleton h={12} w="70%" />
          </Stack>
        ))}
      </SimpleGrid>
    );
  }

  if (items.length === 0) {
    return (
      <Center py="xl">
        <Text c="dimmed" size="sm">
          {emptyMessage}
        </Text>
      </Center>
    );
  }

  const playItem = playId != null ? items.find((item) => item.id === playId) : undefined;

  return (
    <>
      <SelectionBar count={selected.size}>
        <HintedActionIcon
          label={t("playlist.selectAll")}
          variant="default"
          size="sm"
          onClick={() => selectAll(items.map((item) => item.id))}
        >
          <IconSelectAll size={16} />
        </HintedActionIcon>
        <HintedActionIcon
          label={t("playlist.invert")}
          variant="default"
          size="sm"
          onClick={() => invert(items.map((item) => item.id))}
        >
          <IconSquareOff size={16} />
        </HintedActionIcon>
        <Button
          size="xs"
          variant="light"
          leftSection={<IconPlayerPlay size={14} />}
          loading={openPlaylist.isPending}
          disabled={selected.size === 0}
          onClick={handlePlaySelected}
        >
          {t("actions.playSelected")}
        </Button>
        <Button
          size="xs"
          variant="light"
          leftSection={<IconPlaylistAdd size={14} />}
          disabled={selected.size === 0}
          onClick={handleAddToPlaylist}
        >
          {t("actions.addToPlaylist")}
        </Button>
        <Button
          size="xs"
          variant="light"
          leftSection={<IconRefresh size={14} />}
          loading={busy}
          disabled={selected.size === 0}
          onClick={() => batchScrape.mutate({ body: { ids: selectedIds } })}
        >
          {t("actions.batchScrape")}
        </Button>
        <Button
          size="xs"
          variant="light"
          leftSection={<IconStar size={14} />}
          loading={busy}
          disabled={selected.size === 0}
          onClick={() => void batchFavorite()}
        >
          {t("favorite.add")}
        </Button>
        <Button
          size="xs"
          variant="light"
          color="dark"
          leftSection={<IconBan size={14} />}
          loading={busy}
          disabled={selected.size === 0}
          onClick={() => void batchBlacklist()}
        >
          {t("blacklist.add")}
        </Button>
        <Button
          size="xs"
          variant="light"
          color="red"
          leftSection={<IconTrash size={14} />}
          loading={busy}
          disabled={selected.size === 0}
          onClick={() => void handleBatchDelete()}
        >
          {t("actions.batchDelete")}
        </Button>
      </SelectionBar>
      <SimpleGrid cols={cols} spacing="md">
        {items.map((item) => (
          <PosterCard
            key={item.id}
            item={item}
            actorBirthday={actorBirthday}
            selected={selected.has(item.id)}
            onToggleSelect={() => toggleOne(item.id)}
            onPlay={() => handlePlay(item)}
            onOpen={() => open(item.id)}
          />
        ))}
      </SimpleGrid>
      <MetadataViewer
        items={items}
        openId={openId}
        onOpenIdChange={setOpenId}
        onFetchMore={onFetchMore}
      />
      <PlaybackModal
        metadataId={playId}
        title={playItem?.number ?? ""}
        onClose={() => setPlayId(null)}
      />
      <PlaybackQueueModal
        entries={queue}
        title={queue.length === 1 ? queue[0].number : t("playlist.title")}
        onClose={() => setQueue([])}
      />
    </>
  );
}
